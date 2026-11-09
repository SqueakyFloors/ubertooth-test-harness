"""One comprehensive release configuration, as a graph of child runs (#403).

A release gate is every bounded, recurring test a release has to pass, asked
for once and answered once. It is **not** one big suite: the tests it covers
cannot share a run. They want different build options, different firmware on
the board, different fixtures, and three of them write flash. Flattening them
into one ``run-suite`` process would mean one lease set, one firmware
expectation and one report for work that needs several of each, and a failure
anywhere would lose the rest.

So the configuration is a graph of **stages**, and each stage is an ordinary
run group: its own child run per host, its own leases, its own build flags,
its own firmware expectation, its own ``report.md``, its own cleanup. Nothing
about a child run is special, and nothing here reaches a target. What this
module adds is the four things a graph needs that one run does not.

**Which stages there are.** ``CONFIGURATIONS`` holds one named configuration,
``release-comprehensive``, as a tuple of ``Stage`` entries naming a committed
plan and suite. A stage's build options are not written down here: they are
read from what the suite's own tests declare they need
(``stage_build_options``), so a suite that starts needing the Python tools
does not also need an edit here. Two stages are not in it by default: the
programmer-paired bootloader acceptance, which writes the one part of flash
an ordinary run cannot put back and is therefore opt-in, and the transmitting
weak-link range suite, which needs an RF-isolated bench and is marked
optional. The soaks are not stages at all
(``docs/release-testing.md`` section 11).

**What not to run twice.** The fast suite and the main release suite overlap
almost entirely: the same test at ``coverage: smoke`` in one and
``coverage: comprehensive`` in the other. Running both would spend minutes
re-proving the cheaper half of each test. ``superseded_tests`` drops from a
stage marked ``drop_superseded`` every test another enabled stage runs at the
same coverage or better, which leaves the fast stage carrying the checks that
are only in it -- and still carrying the build, the flash and the preflight of
every host, which is what makes it the cheap place for a failure.

**What may overlap.** A target runner runs one run at a time (the controller
refuses a second: ``Controller._request_run``), so two stages that name the
same host can never overlap, whatever hardware they ask for. Every fixture in
a registry belongs to a host, so two stages on *different* hosts share no
board, no peer pair and no power controller, and the firmware/power lock is
per host as well. That makes the rule simple and exact: stages may overlap
only when their host sets are disjoint, and ``schedule`` lays them out in
waves that way. On a bench with one Linux host the result is a sequence, which
is the honest answer rather than a schedule that would have two runs fighting
over one board; a Mac rides along in the stages whose plans say
``hosts: each``.

Ordering on one host is not left to chance either. The fast stage goes first.
The stages that write flash go last, in the order
``docs/release-testing.md`` section 6 sets: ``btbr`` (which writes the
alternate image and puts the application image back), then the firmware image
acceptance, then the bootloader. Each writer *requires* the one before it, so
a board whose firmware state is in doubt is never written again; every other
stage requires only the fast stage, so one capture failing does not abandon
the next capture.

**One verdict.** ``gate_view`` turns the stored record and its children's
group views into one stage matrix -- each stage's run IDs, the commits under
test, the device roles it bound, when it started and ended, its duration in
seconds and in words, its verdict, its reason, and where its report and
records are -- and one overall verdict, which is ``passed`` only when every
required stage passed.

What this module does not decide: a stage's own result (its run does), what a
bench has (the target's registry does), and whether to start at all
(``preflight`` reports what it can decide before anything is leased or built,
and the caller refuses). It holds no I/O but reading the committed plan files.
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
import shutil
from typing import Mapping, Sequence
import uuid

from ubertooth_harness.catalog import CatalogError, TestCatalog, builtin_catalog
from ubertooth_harness.durations import human_duration_or_none
from ubertooth_harness.gui import _build_needs, omitted_reason, run_plan_check
from ubertooth_harness.plan import PlanError, load_plan
from ubertooth_harness.protocol import _identifier
from ubertooth_harness.rungroups import (
    BLOCKED,
    CANCELED,
    ERROR,
    FAILED,
    MAX_REASON_CHARS,
    NOT_REACHED,
    PASSED,
    PENDING,
    RUNNING,
    SKIPPED,
    wall_seconds,
    worst,
)


class ReleaseGateError(ValueError):
    """The configuration, or a request against it, cannot be carried out."""


# Gate records live beside the run directories, one file per gate, as group
# records do.
GATES_DIR = "release-gates"
# A gate ID is neither a run ID (those start with a UTC timestamp) nor a
# group ID (``grp-``).
GATE_ID_PREFIX = "gate-"

# The one named configuration this module offers.
COMPREHENSIVE = "release-comprehensive"

# The wall time the whole non-soak gate is meant to stay inside, so that it
# and the separate four-hour soak keep the release procedure under six hours
# (docs/release-testing.md section 6). Exceeding it is a procedure failure to
# correct, not a reason to refuse a gate or drop a stage, so preflight
# reports it as a warning.
TARGET_WALL_SECONDS = 2 * 60 * 60.0
# What the controller's own results directory must have free before a gate
# starts. The gate's run directories are the children's, and each child is an
# ordinary run, so this is the controller's share: one run.jsonl and one
# report per stage per host, with room to spare.
MIN_FREE_BYTES = 512 * 1024 * 1024

# How a stage asks for its boards' bootloaders, as ``run-suite --bootloader``
# names it. Only the bootloader stage asks for anything but the first.
BOOTLOADER_RECORD = "record"
BOOTLOADER_UPDATE = "update"

# Coverage profiles, worst first, as a plan names them. A stage's test is
# superseded by another stage's run of the same test at this coverage or
# better; a test with no coverage argument has one profile and is superseded
# by any other run of it.
COVERAGE_ORDER = ("smoke", "comprehensive")
# The plan argument that names one.
COVERAGE_ARGUMENT = "coverage"

# A stage status of its own, beside the ones a group can reach
# (rungroups.RANK_ORDER): a stage this bench has nothing to run and that the
# configuration marks optional, or one the opt-in was not given for. It is
# ``skipped``, which never fails a gate.
# A stage abandoned because a stage it requires failed is ``not_reached``.

# The stage statuses that mean "this stage is over and it did not pass".
BAD = (FAILED, BLOCKED, CANCELED, ERROR, NOT_REACHED)
# The statuses a gate is still working through.
UNFINISHED = (PENDING, RUNNING)


# ----- the configuration ---------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Stage:
    """One stage of a release configuration: a committed plan and suite.

    ``order`` sorts the stages that write flash after those that do not, and
    those among themselves; ``requires`` names the stages whose failure
    abandons this one. ``optional`` marks a stage a bench may lack the
    fixture for without blocking the gate, and ``opt_in`` one that is left
    out unless the operator asks for it by name.

    ``tests`` names the exact tests of the suite to run, for a stage that
    wants a part of one; empty means the whole suite. ``drop_superseded``
    asks for the suite minus every test another enabled stage runs at the
    same coverage or better (``superseded_tests``).
    """

    name: str
    plan: str
    suite: str
    purpose: str
    order: int = 0
    requires: tuple[str, ...] = ()
    optional: bool = False
    opt_in: bool = False
    tests: tuple[str, ...] = ()
    drop_superseded: bool = False
    bootloader: str = BOOTLOADER_RECORD
    # A test of this stage writes a board's flash, so it goes after the
    # stages that need the run's own firmware on that board, and nothing
    # runs on a board after it but another writer.
    writes_firmware: bool = False
    # This stage leaves the alternate image on its board during the run and
    # writes the application image back before it ends. The stage after it
    # flashes and preflights the application image again, which is what
    # verifies the restoration rather than taking the stage's word for it.
    restores_application_image: bool = False
    # Why this stage is optional or opt-in, for the matrix and the docs.
    condition: str = ""


# The fast stage, first, so a build, a flash, a preflight or a host-tool
# failure is found in seconds rather than in the middle of the release suite.
_FAST = "fast-only"

RELEASE_COMPREHENSIVE: tuple[Stage, ...] = (
    Stage(
        name=_FAST,
        plan="fast", suite="fast", drop_superseded=True,
        purpose="every host's build, flash and preflight, with the checks "
                "the fast suite alone has",
    ),
    Stage(
        name="release", plan="release", suite="release", requires=(_FAST,),
        purpose="the main non-soak release suite, including the AFH map "
                "recovery the peers measure",
    ),
    Stage(
        name="ble", plan="ble", suite="ble-comprehensive", requires=(_FAST,),
        purpose="BLE comprehensive: the known advertiser on every "
                "advertising channel, the extcap and tshark paths, and the "
                "known connection",
    ),
    Stage(
        name="classic", plan="classic", suite="classic-comprehensive",
        requires=(_FAST,),
        purpose="Classic comprehensive: the known piconet captured, "
                "followed, decoded field by field, and its AFH map recovered",
    ),
    Stage(
        name="hci", plan="hci", suite="hci-comprehensive", requires=(_FAST,),
        purpose="HCI comprehensive: inquiry scan and piconet follow through "
                "the host's own Bluetooth adapter",
    ),
    Stage(
        name="ble-weak-link", plan="ble", suite="ble-weak-link",
        requires=(_FAST,),
        purpose="the known advertiser read by a receiver behind 20 dB and "
                "behind 40 dB",
    ),
    Stage(
        name="connection-weak-link", plan="ble", suite="connection-weak-link",
        requires=(_FAST,),
        purpose="the known connection captured at 40 dB and followed across "
                "its end at 20 dB",
    ),
    Stage(
        name="classic-weak-link", plan="classic", suite="classic-weak-link",
        requires=(_FAST,),
        purpose="the known piconet followed by a receiver behind 20 dB",
    ),
    Stage(
        name="range-weak-link", plan="range", suite="range-weak-link",
        requires=(_FAST,), optional=True,
        condition="it transmits between two boards and wants an RF-isolated "
                  "bench, and its suite has not yet judged a result "
                  "(docs/tests.md, Two-device range test)",
        purpose="the two-device range test with the responder behind 20 dB",
    ),
    # The writers, in the order section 6 of docs/release-testing.md sets.
    Stage(
        name="btbr", plan="classic", suite="btbr-comprehensive", order=1,
        requires=(_FAST,), writes_firmware=True,
        restores_application_image=True,
        purpose="the alternate btbr image: the ubertooth-btbr console on it, "
                "then the application image written back and verified",
    ),
    Stage(
        name="application-firmware", plan="firmware", suite="firmware",
        order=2, requires=(_FAST, "btbr"), writes_firmware=True,
        purpose="application firmware acceptance: suffixing, read-back, every "
                "bad image refused before a write, and the image verified "
                "at the end",
    ),
    Stage(
        name="bootloader", plan="fast", suite="fast",
        tests=("Ubertooth identity",), order=3,
        requires=(_FAST, "application-firmware"),
        opt_in=True, writes_firmware=True, bootloader=BOOTLOADER_UPDATE,
        condition="it writes the one part of flash an ordinary run cannot "
                  "put back, so it runs only on a board the registry pairs "
                  "with a debug programmer, and only when asked for by name",
        purpose="bootloader acceptance: the bootloader built from this commit "
                "installed over the paired programmer, then read back with "
                "the application image running again",
    ),
)

CONFIGURATIONS: Mapping[str, tuple[Stage, ...]] = {
    COMPREHENSIVE: RELEASE_COMPREHENSIVE,
}

# Release gates nothing here measures, named in every gate's summary so that
# a gate it has no stage for is not mistaken for a gate nobody owes. Each is
# run by hand and recorded with ``release-evidence --attest``.
MANUAL_GATES: tuple[Mapping[str, str], ...] = (
    {
        "name": "installer acceptance",
        "reference": "docs/release-testing.md TC-16",
        "reason": "the candidate's own install.sh is run on a clean prefix by "
                  "hand; the harness never calls it, so no suite covers it",
    },
)

# Bounded release tests that are deliberately not stages, with the reason, so
# the configuration's edges are written down rather than inferred from its
# absences.
EXCLUDED: tuple[Mapping[str, str], ...] = (
    {
        "name": "slow / mac (flash reliability)",
        "reason": "it alternates against a pinned known-good image, which "
                  "only run-suite --artifact can pass and a dispatched run "
                  "cannot",
    },
    {
        "name": "exclusive",
        "reason": "it switches real USB power, which is armed on the runner "
                  "(--allow-power-changes) and never by a request",
    },
    {
        "name": "power",
        "reason": "it leaves a board outside its application until its own "
                  "recovery step, so somebody has to be at the bench "
                  "(run-suite --allow-destructive)",
    },
    {
        "name": "classic-long, btbr-long",
        "reason": "the bounded long variants repeat a comprehensive stage's "
                  "measurement for longer; they are run on their own, not in "
                  "the recurring gate",
    },
    {
        "name": "the soaks",
        "reason": "no soak is a release gate (docs/release-testing.md "
                  "section 11); the four-hour soak runs beside this gate and "
                  "the 72-hour endurance run is optional",
    },
)


def configuration(name: str) -> tuple[Stage, ...]:
    """The named configuration's stages, or ReleaseGateError."""
    try:
        return CONFIGURATIONS[name]
    except (KeyError, TypeError):
        raise ReleaseGateError(
            f"unknown release configuration {name!r}; this harness has "
            + ", ".join(sorted(CONFIGURATIONS))
        ) from None


def enabled(stages: Sequence[Stage], *, with_bootloader: bool = False) -> tuple[Stage, ...]:
    """The stages a gate runs: the opt-in ones only when asked for.

    ``with_bootloader`` is the one opt-in today, and it is asked for by the
    operator rather than worked out: whether a board is paired with a
    programmer is in the target's registry, which nothing here can read, and
    a stage that writes a bootloader is not something to start on a guess.
    """
    return tuple(
        stage for stage in stages
        if not stage.opt_in or (with_bootloader and stage.bootloader == BOOTLOADER_UPDATE)
    )


# ----- reading the plans ---------------------------------------------------


def _coverage_rank(arguments: Mapping) -> int:
    """How thorough a plan step is, as an index into COVERAGE_ORDER.

    A step with no coverage argument has one profile, so it ranks with the
    most thorough: another stage's run of the same test cannot be less.
    """
    value = arguments.get(COVERAGE_ARGUMENT)
    if not isinstance(value, str) or value not in COVERAGE_ORDER:
        return len(COVERAGE_ORDER) - 1
    return COVERAGE_ORDER.index(value)


def suite_steps(plans_dir: Path | str, stage: Stage, catalog: TestCatalog):
    """One stage's plan steps, or ReleaseGateError naming what is wrong.

    Read the way a runner reads a plan: an exact ``<plan>.yaml`` in the plans
    directory. A plan that does not load, a suite it has not got, and a plan
    of planned tests are each refused here, before anything is leased: the
    configuration names committed suites, and one of them going missing is a
    mistake in this repository rather than something a bench can answer.
    """
    path = Path(plans_dir) / f"{stage.plan}.yaml"
    try:
        plan = load_plan(path, catalog)
    except (PlanError, CatalogError, OSError) as error:
        raise ReleaseGateError(
            f"stage {stage.name}: cannot read {stage.plan}.yaml: {error}"
        ) from None
    if not plan.runnable:
        raise ReleaseGateError(
            f"stage {stage.name}: {stage.plan}.yaml is a plan of planned "
            "tests (runnable: false), which no run may name"
        )
    try:
        steps = plan.steps(stage.suite)
    except PlanError as error:
        raise ReleaseGateError(f"stage {stage.name}: {error}") from None
    if stage.tests:
        unknown = [name for name in stage.tests
                   if not any(step.test_name == name for step in steps)]
        if unknown:
            raise ReleaseGateError(
                f"stage {stage.name}: suite {stage.suite} has no "
                + ", ".join(repr(name) for name in unknown)
            )
    return steps


def superseded_tests(
    stages: Sequence[Stage],
    plans_dir: Path | str,
    catalog: TestCatalog,
    hosts_of: Mapping[str, Sequence[str]],
    platforms: Mapping[str, str | None],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """What each ``drop_superseded`` stage leaves to another stage, and to which.

    One entry per such stage: ``(test name, the stage that covers it)`` for
    every test another enabled stage runs at the same coverage or better, on
    every host this stage would have run it on. What is left is the stage's
    own selection (``stage_tests``).

    Two things decide it, and both have to hold. Coverage: two stages running
    one test at the same coverage are the same measurement twice, so the
    later stage keeps it and the earlier one drops it, and a smoke case whose
    comprehensive profile is elsewhere is a case that profile already
    includes. And the hosts: a test covered on the Linux host alone is not
    covered for the Mac, so dropping it from a stage that runs on both hosts
    would quietly lose the Mac's measurement. A test nobody else runs
    everywhere this stage runs it stays, whatever it costs.
    """
    best: dict[tuple, dict[str, tuple[int, str]]] = {}
    for stage in stages:
        if stage.drop_superseded:
            continue
        for step in suite_steps(plans_dir, stage, catalog):
            if stage.tests and step.test_name not in stage.tests:
                continue
            rank = _coverage_rank(step.arguments)
            per_host = best.setdefault(_test_key(step), {})
            for host in _step_hosts(
                step, hosts_of.get(stage.name, ()), platforms, catalog
            ):
                held = per_host.get(host)
                if held is None or rank > held[0]:
                    per_host[host] = (rank, stage.name)
    dropped: dict[str, tuple[tuple[str, str], ...]] = {}
    for stage in stages:
        if not stage.drop_superseded:
            continue
        entries = []
        for step in suite_steps(plans_dir, stage, catalog):
            rank = _coverage_rank(step.arguments)
            per_host = best.get(_test_key(step), {})
            mine = _step_hosts(
                step, hosts_of.get(stage.name, ()), platforms, catalog
            )
            covered = [
                per_host[host][1] for host in mine
                if host in per_host and per_host[host][0] >= rank
            ]
            if mine and len(covered) == len(mine):
                entries.append((step.test_name, sorted(set(covered))[0]))
        dropped[stage.name] = tuple(entries)
    return dropped


# The arguments that say where a step runs rather than what it measures, and
# so are not part of what makes two steps the same measurement. Where a step
# runs is decided separately, host by host (``superseded_tests``).
_PLACEMENT_ARGUMENTS = frozenset({
    COVERAGE_ARGUMENT, "hosts", "host", "platforms", "required hardware",
})


def _test_key(step) -> tuple:
    """What makes two plan steps the same measurement, bar the coverage.

    The test's name and every argument it was given that decides what it
    measures, so one stage's case is superseded only by another stage's run
    of the *same* case at the same coverage or better. Two steps of one test
    that differ in such an argument -- a different register, a different
    attenuation -- measure different things, and neither covers the other.
    """
    return (
        step.test_name,
        tuple(sorted(
            (name, repr(value)) for name, value in step.arguments.items()
            if name not in _PLACEMENT_ARGUMENTS
        )),
    )


def _step_hosts(
    step, hosts: Sequence[str], platforms: Mapping[str, str | None],
    catalog: TestCatalog,
) -> tuple[str, ...]:
    """Which of these hosts a run of this plan step would run it on."""
    return tuple(
        host for host in hosts
        if not omitted_reason(
            catalog, step.test_name, step.arguments, host, platforms.get(host)
        )
    )


def stage_tests(
    stage: Stage, steps, dropped: Sequence[tuple[str, str]] = ()
) -> tuple[str, ...]:
    """The test names this stage's runs select, in the suite's own order.

    Empty for a stage that runs its whole suite, which is how a run says "all
    of it" and keeps a suite that gains a test from needing an edit here.
    """
    if stage.tests:
        return tuple(stage.tests)
    if not stage.drop_superseded:
        return ()
    gone = {name for name, _covered_by in dropped}
    kept: list[str] = []
    for step in steps:
        if step.test_name in gone or step.test_name in kept:
            continue
        kept.append(step.test_name)
    return tuple(kept)


def stage_build_options(steps, tests: Sequence[str], catalog: TestCatalog) -> dict:
    """The build options a stage's selected tests need, from the catalog.

    ``build`` and ``flash_build`` are true for every stage: a release is
    qualified on tools and firmware built from the commit under test, and the
    boards carry that run's own firmware, which its preflight then verifies.
    That is also what verifies a ``btbr`` stage put the application image
    back: the stage after it on that board writes the application image and
    preflights its identity.

    The other three are read from what the suite's own tests declare
    (``gui._build_needs``), so a suite that starts needing the Python tools
    or the alternate image does not also need an edit to the configuration.
    ``needs_artifacts`` is every image no build makes, which only
    ``run-suite --artifact`` can pass: a dispatched run cannot, so preflight
    refuses a stage that names one.
    """
    chosen = set(tests)
    firmware = btbr = python = False
    artifacts: set[str] = set()
    for step in steps:
        if chosen and step.test_name not in chosen:
            continue
        needs = _build_needs(catalog, step.test_name, step.arguments)
        firmware = firmware or needs["needs_firmware_build"]
        btbr = btbr or needs["needs_btbr_build"]
        python = python or needs["needs_python_tools"]
        artifacts.update(needs["needs_artifacts"])
    return {
        "build": True, "flash_build": True,
        "with_firmware": bool(firmware), "with_btbr": bool(btbr),
        "with_python": bool(python),
        "needs_artifacts": sorted(artifacts),
    }


def _decibels(value) -> str:
    """An attenuation as a plan's requirement names it: 20, not 20.0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def stage_resources(steps, tests: Sequence[str], catalog: TestCatalog) -> tuple[str, ...]:
    """The device roles this stage binds, as the plan names them.

    ``Ubertooth: default``, ``acceptor: default``, ``Ubertooth: 20 dB`` for a
    ``weak link:`` receiver. Role aliases as the plan writes them, never a
    serial or a registered alias: the registry is the target's, and this is
    what the matrix reports.
    """
    chosen = set(tests)
    found: list[str] = []
    for step in steps:
        if chosen and step.test_name not in chosen:
            continue
        try:
            roles = catalog.get(step.test_name).hardware_roles or {}
        except CatalogError:
            continue
        weak = step.arguments.get("weak link") or {}
        for role in roles:
            selector = step.arguments.get(role)
            if selector is None:
                continue
            if isinstance(weak, Mapping) and role in weak:
                entry = f"{role}: {_decibels(weak[role])} dB"
            else:
                entry = f"{role}: {selector}"
            if entry not in found:
                found.append(entry)
    return tuple(found)


def stage_estimate(steps, tests: Sequence[str], catalog: TestCatalog) -> float | None:
    """The catalog's seconds for this stage's selected tests on one host.

    None when one of them has no estimate: a total that silently left a test
    out would read as a shorter gate than the one being asked for. Build,
    flash and preflight are not here; the caller adds what the hosts have
    learned those take (``preflight``).
    """
    chosen = set(tests)
    total = 0.0
    for step in steps:
        if chosen and step.test_name not in chosen:
            continue
        try:
            definition = catalog.get(step.test_name)
            seconds = definition.estimated_seconds(
                definition.normalize_arguments(step.arguments)
            )
        except (CatalogError, TypeError, ValueError):
            return None
        if seconds is None:
            return None
        total += seconds
    return round(total, 1)


# ----- compiling one gate --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StagePlan:
    """One stage as a gate would start it: the request, and what it costs.

    Everything a child group needs (``request``) and everything the matrix
    reports about the stage itself. ``hosts`` is the gate's hosts that run a
    step of the suite; ``refusal`` is why none of them could, which blocks the
    gate unless the stage is optional.
    """

    stage: Stage
    hosts: tuple[str, ...]
    tests: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...]
    resources: tuple[str, ...]
    estimated_s: float | None
    build: bool
    flash_build: bool
    with_firmware: bool
    with_btbr: bool
    with_python: bool
    needs_artifacts: tuple[str, ...]
    refusal: str = ""

    @property
    def name(self) -> str:
        return self.stage.name

    @property
    def runnable(self) -> bool:
        return bool(self.hosts) and not self.refusal

    def request(self) -> dict:
        """The fields a child run group is started with."""
        return {
            "hosts": list(self.hosts),
            "plan": self.stage.plan,
            "suite": self.stage.suite,
            "tests": list(self.tests),
            "build": self.build,
            "flash_build": self.flash_build,
            "with_firmware": self.with_firmware,
            "with_btbr": self.with_btbr,
            "with_python": self.with_python,
            "bootloader": self.stage.bootloader,
        }

    def record(self) -> dict:
        """The stage as a gate record stores it.

        Stored rather than worked out again when the gate is read, so a gate
        reports the stages it was started with even after the configuration
        or a plan file changes under it.
        """
        return {
            "stage": self.stage.name,
            "purpose": self.stage.purpose,
            "condition": self.stage.condition,
            "optional": self.stage.optional,
            "opt_in": self.stage.opt_in,
            "writes_firmware": self.stage.writes_firmware,
            "restores_application_image": self.stage.restores_application_image,
            "requires": list(self.stage.requires),
            "order": self.stage.order,
            "resources": list(self.resources),
            "dropped_tests": [
                {"test": name, "covered_by": covered} for name, covered in self.dropped
            ],
            "estimated_s": self.estimated_s,
            "refusal": self.refusal,
            **self.request(),
        }


def eligible_hosts(
    steps, tests: Sequence[str], hosts: Sequence[str],
    platforms: Mapping[str, str | None], catalog: TestCatalog,
) -> tuple[tuple[str, ...], str]:
    """The hosts that run a step of this suite, and why none of them does.

    Host by host first, because a stage runs on the hosts that have something
    to run and not on the others, and then the chosen set together: a suite
    whose required step names a host outside it is refused as a whole, which
    is the same judgement ``Controller.start_group`` makes
    (``gui.run_plan_check``).
    """
    chosen = tuple(
        host for host in hosts
        if run_plan_check(
            catalog, steps, (host,), platforms={host: platforms.get(host)},
            tests=list(tests),
        )["runs"]
    )
    if not chosen:
        reasons = [
            run_plan_check(
                catalog, steps, (host,), platforms={host: platforms.get(host)},
                tests=list(tests),
            )["refusal"]
            for host in hosts
        ]
        named = next((reason for reason in reasons if reason), "")
        return (), named or (
            "no host was chosen for this gate" if not hosts
            else "no host of this gate runs a step of this suite"
        )
    refusal = run_plan_check(
        catalog, steps, list(chosen),
        platforms={host: platforms.get(host) for host in chosen},
        tests=list(tests),
    )["refusal"]
    return chosen, refusal


def compile_stages(
    stages: Sequence[Stage],
    plans_dir: Path | str,
    hosts: Sequence[str],
    *,
    platforms: Mapping[str, str | None] | None = None,
    catalog: TestCatalog | None = None,
) -> tuple[StagePlan, ...]:
    """Every stage as it would be started on these hosts, in configuration order.

    Nothing here asks a target anything: the plans, the catalog, the hosts
    the caller chose and their platforms are all of it. What a bench has is
    the target's own answer, and its run reports it.
    """
    catalog = catalog or builtin_catalog()
    platforms = dict(platforms or {})
    steps_of = {
        stage.name: suite_steps(plans_dir, stage, catalog) for stage in stages
    }
    # Two passes, because what one stage leaves to another depends on where
    # each of them runs: first where every stage's whole suite would run,
    # then the selection that follows from it, then the hosts again for the
    # stages whose selection changed.
    hosts_of = {
        stage.name: eligible_hosts(
            steps_of[stage.name], stage.tests, hosts, platforms, catalog
        )[0]
        for stage in stages
    }
    dropped = superseded_tests(stages, plans_dir, catalog, hosts_of, platforms)
    compiled: list[StagePlan] = []
    for stage in stages:
        steps = steps_of[stage.name]
        left = dropped.get(stage.name, ())
        tests = stage_tests(stage, steps, left)
        chosen, refusal = eligible_hosts(steps, tests, hosts, platforms, catalog)
        options = stage_build_options(steps, tests, catalog)
        compiled.append(StagePlan(
            stage=stage, hosts=chosen, tests=tests, dropped=left,
            resources=stage_resources(steps, tests, catalog),
            estimated_s=stage_estimate(steps, tests, catalog),
            needs_artifacts=tuple(options.pop("needs_artifacts")),
            refusal=refusal, **options,
        ))
    return tuple(compiled)


# ----- scheduling ----------------------------------------------------------


def schedule(plans: Sequence[StagePlan]) -> tuple[tuple[str, ...], ...]:
    """The stages in waves: one wave's stages may run at the same time.

    Two stages may share a wave only when their host sets are disjoint. A
    target runner runs one run at a time, and every fixture a registry holds
    belongs to a host, so stages on different hosts share no board, no peer
    pair, no power controller and no firmware lock, and stages on the same
    host cannot overlap however little they share.

    Within that, the order is the configuration's: a stage waits for the
    stages it requires, and a stage that writes flash waits for every stage
    of a lower ``order`` that shares a host with it. Deterministic, and in
    configuration order inside a wave, so the same gate always reads the
    same way.

    Stages with nothing to run (no eligible host) are not scheduled: they are
    skipped or they block the gate, which ``preflight`` decides.
    """
    waiting = [plan for plan in plans if plan.runnable]
    placed: dict[str, int] = {}
    waves: list[list[str]] = []
    while waiting:
        wave: list[StagePlan] = []
        for plan in waiting:
            if any(
                name not in placed for name in plan.stage.requires
                if any(other.name == name for other in plans if other.runnable)
            ):
                continue
            if any(
                other.stage.order < plan.stage.order
                and set(other.hosts) & set(plan.hosts)
                and other.name not in placed
                for other in waiting
            ):
                continue
            if any(set(other.hosts) & set(plan.hosts) for other in wave):
                continue
            wave.append(plan)
        if not wave:
            # A requirement that is not in the graph, or a ring of them. The
            # configuration is this repository's, so this is a mistake here
            # rather than something a bench can answer.
            raise ReleaseGateError(
                "the configuration's stages cannot be ordered: "
                + ", ".join(plan.name for plan in waiting)
            )
        waves.append([plan.name for plan in wave])
        for plan in wave:
            placed[plan.name] = len(waves)
        waiting = [plan for plan in waiting if plan.name not in placed]
    return tuple(tuple(wave) for wave in waves)


def wall_estimate(
    plans: Sequence[StagePlan],
    waves: Sequence[Sequence[str]],
    overhead_s: Mapping[str, float] | None = None,
) -> float | None:
    """How long the whole gate is estimated to take, wave by wave.

    Each wave costs its slowest stage, since a wave's stages run at once, and
    each stage costs its tests plus what that stage's hosts have learned a
    build, a flash and a preflight take (``overhead_s`` per host; the caller
    reads it from the estimate store). None when a stage has no estimate: a
    total with a test silently left out would read as a shorter gate than the
    one being asked for.
    """
    by_name = {plan.name: plan for plan in plans}
    total = 0.0
    for wave in waves:
        longest = 0.0
        for name in wave:
            plan = by_name.get(name)
            if plan is None:
                continue
            if plan.estimated_s is None:
                return None
            cost = plan.estimated_s + max(
                (float((overhead_s or {}).get(host, 0.0)) for host in plan.hosts),
                default=0.0,
            )
            longest = max(longest, cost)
        total += longest
    return round(total, 1)


# ----- preflighting the whole graph ----------------------------------------


# The preflight checks, by name, so a caller can leave out or find one.
IMMUTABLE_REVISION = "immutable revision"
REQUIRED_STAGE_HOSTS = "every required stage has a host"
NO_PASSED_IN_IMAGE = "no stage needs a passed-in image"
BOOTLOADER_ACCEPTANCE = "bootloader acceptance"
BOARD_ON_EVERY_HOST = "a board on every stage's host"
DISK_BUDGET = "disk budget"
WALL_TIME = "estimated wall time"


@dataclasses.dataclass(frozen=True)
class GateCheck:
    """One preflight judgement on the whole graph.

    ``blocking`` marks a check that must not be started around: a gate with
    one is refused before anything is leased, built or written. A check that
    is not blocking is a warning the answer carries.
    """

    name: str
    passed: bool
    detail: str
    blocking: bool = True

    def view(self) -> dict:
        return dataclasses.asdict(self)


def _commit_check(ref: str, commit: str | None) -> GateCheck:
    """The gate runs one immutable revision, named once.

    A gate is hours of work on several hosts; a branch that moves in the
    middle would leave stages testing different code and one summary
    claiming they tested the same. So the ref is resolved to a commit before
    the first stage and every child is sent that commit (the controller
    resolves it once, as it does for a run group).
    """
    if commit and len(commit) >= 40 and all(
        character in "0123456789abcdef" for character in commit.lower()
    ):
        return GateCheck(
            IMMUTABLE_REVISION, True,
            f"every stage runs {commit[:12]}, resolved once from {ref}",
        )
    return GateCheck(
        IMMUTABLE_REVISION, False,
        f"{ref} did not resolve to a commit, so the stages could not be "
        "pinned to one revision",
    )


def _disk_check(results_dir: Path | str | None, minimum: int) -> GateCheck:
    """Room for the records of every stage of every host."""
    if results_dir is None:
        return GateCheck(
            DISK_BUDGET, True, "no results directory to judge", blocking=False
        )
    try:
        free = shutil.disk_usage(Path(results_dir)).free
    except OSError as error:
        return GateCheck(DISK_BUDGET, False, f"cannot read the free space: {error}")
    if free >= minimum:
        return GateCheck(
            DISK_BUDGET, True,
            f"{free // (1024 * 1024)} MiB free where the records are kept",
        )
    return GateCheck(
        DISK_BUDGET, False,
        f"only {free // (1024 * 1024)} MiB free where the records are kept; "
        f"a gate wants {minimum // (1024 * 1024)} MiB",
    )


def _board_check(
    plans: Sequence[StagePlan], boards: Mapping[str, int] | None
) -> GateCheck:
    """A registered board on every host a stage of this gate would run on.

    The one piece of a bench the controller itself knows: each target reports
    the boards its registry holds and whether USB has them. A host with none
    runs no radio stage at all, and every stage here binds an Ubertooth, so
    that is worth refusing before a build rather than discovering per stage.

    Everything else a stage needs -- the ESP32 pair, the DKs, the host's
    Bluetooth adapter, the attenuated board, the debug programmer -- is in
    the target's own resource registry, which the controller does not hold
    and does not ask for. Those are the target's answer, and a required
    stage that cannot be measured for one of them fails the gate rather than
    passing it unmeasured. ``docs/guide/release-gate.md`` says so where an
    operator reads it.
    """
    if boards is None:
        return GateCheck(
            BOARD_ON_EVERY_HOST, True,
            "not judged here: no target reported its boards", blocking=False,
        )
    wanted = sorted({
        host for plan in plans if plan.runnable and not plan.stage.optional
        for host in plan.hosts
    })
    empty = [host for host in wanted if not boards.get(host)]
    if empty:
        return GateCheck(
            BOARD_ON_EVERY_HOST, False,
            ", ".join(empty) + " has no registered Ubertooth that USB can "
            "see, and every stage of this gate binds one",
        )
    return GateCheck(
        BOARD_ON_EVERY_HOST, True,
        "; ".join(f"{host}: {boards.get(host)} board(s)" for host in wanted)
        or "no host to judge",
    )


def preflight(
    plans: Sequence[StagePlan],
    waves: Sequence[Sequence[str]],
    *,
    ref: str,
    commit: str | None,
    with_bootloader: bool = False,
    results_dir: Path | str | None = None,
    overhead_s: Mapping[str, float] | None = None,
    boards: Mapping[str, int] | None = None,
    minimum_free_bytes: int = MIN_FREE_BYTES,
    target_wall_s: float = TARGET_WALL_SECONDS,
) -> dict:
    """Judge the complete graph before any part of it runs.

    Everything that can be decided without leasing a board, building a
    commit or writing a flash is decided here, and a gate with one blocking
    failure starts nothing: a graph that would stop halfway through has
    occupied the bench, written firmware and produced a verdict nobody can
    use.

    What is judged: that the ref resolved to one immutable commit; that every
    stage's plan and suite loaded (``compile_stages`` refuses otherwise);
    that every required stage has a host that runs it, and that no required
    stage would leave a gate unable to measure what its suite requires; that
    no stage needs an image only ``--artifact`` can pass; that the opt-in
    bootloader stage was asked for before its fixture is relied on; that
    every host a required stage runs on has a registered board USB can see
    (``boards``, which is what the targets reported); that the records have
    room; and what the whole gate is estimated to take against the
    procedure's own target.

    What is not judged here, because only the target's own resource registry
    knows it: the peers, the DKs, the host adapter, the attenuated board and
    the debug programmer. A stage whose fixture is absent is the target's
    refusal, and a required stage that fails for it fails the gate rather
    than passing it unmeasured.
    """
    checks: list[GateCheck] = [_commit_check(ref, commit)]
    missing = [plan for plan in plans if not plan.runnable]
    blocked = [plan for plan in missing if not plan.stage.optional]
    if blocked:
        checks.append(GateCheck(
            REQUIRED_STAGE_HOSTS, False,
            "; ".join(
                f"{plan.name}: {plan.refusal}" for plan in blocked
            ),
        ))
    else:
        checks.append(GateCheck(
            REQUIRED_STAGE_HOSTS, True,
            f"{sum(plan.runnable for plan in plans)} of {len(plans)} stages "
            "have a host that runs them"
            + (
                "; skipping " + ", ".join(plan.name for plan in missing)
                if missing else ""
            ),
        ))
    artifacts = [plan for plan in plans if plan.runnable and plan.needs_artifacts]
    checks.append(GateCheck(
        NO_PASSED_IN_IMAGE, not artifacts,
        "; ".join(
            f"{plan.name} needs {', '.join(plan.needs_artifacts)}, which only "
            "run-suite --artifact can pass"
            for plan in artifacts
        ) or "no stage names an image a build does not make",
    ))
    paired = [plan for plan in plans if plan.stage.bootloader == BOOTLOADER_UPDATE]
    checks.append(GateCheck(
        BOOTLOADER_ACCEPTANCE, True,
        (
            "asked for: it writes a bootloader over the debug programmer the "
            "registry pairs with a board, and the run skips it where no board "
            "is paired"
            if paired and with_bootloader else
            "not asked for; the installed bootloader of every board is still "
            "recorded by each stage"
        ),
        blocking=False,
    ))
    checks.append(_board_check(plans, boards))
    checks.append(_disk_check(results_dir, minimum_free_bytes))
    estimated = wall_estimate(plans, waves, overhead_s)
    if estimated is None:
        checks.append(GateCheck(
            WALL_TIME, True,
            "not estimated: a stage holds a test the catalog has no estimate for",
            blocking=False,
        ))
    else:
        checks.append(GateCheck(
            WALL_TIME, estimated <= target_wall_s,
            f"about {human_duration_or_none(estimated)} over "
            f"{len(waves)} wave(s), against a target of "
            f"{human_duration_or_none(target_wall_s)}",
            blocking=False,
        ))
    failures = [check for check in checks if not check.passed and check.blocking]
    return {
        "checks": [check.view() for check in checks],
        "estimated_s": estimated,
        "estimated_hms": human_duration_or_none(estimated),
        "waves": [list(wave) for wave in waves],
        "passed": not failures,
        "refusal": "; ".join(f"{check.name}: {check.detail}" for check in failures),
        "warnings": [
            f"{check.name}: {check.detail}" for check in checks
            if not check.passed and not check.blocking
        ],
    }


# ----- the gate record -----------------------------------------------------


_GATE_FIELDS = (
    "gate_id", "config", "hosts", "ref", "commit", "with_bootloader",
    "created_at", "dispatched_at", "waves", "stages", "cancel_requested",
    "cancel_reason",
)

# What a gate may record about a stage it decided not to run, beside a
# status its child group reached on its own.
STAGE_OUTCOMES = (SKIPPED, NOT_REACHED, CANCELED, FAILED, ERROR)


def new_gate_id() -> str:
    return GATE_ID_PREFIX + uuid.uuid4().hex


def valid_gate_id(value) -> bool:
    return _identifier(value)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds"
    )


@dataclasses.dataclass
class ReleaseGate:
    """One asked-for gate: its configuration, its hosts, and each stage's group.

    The record holds what was asked for and which child group each stage
    became. Everything else -- a stage's verdict, the gate's verdict, when it
    finished, how long it took -- is derived from the children's own records
    (``gate_view``), so no state machine here has to be kept in step with
    them.
    """

    gate_id: str
    config: str = COMPREHENSIVE
    hosts: list = dataclasses.field(default_factory=list)
    ref: str | None = None
    commit: str | None = None
    with_bootloader: bool = False
    created_at: str = dataclasses.field(default_factory=_now)
    # When the last stage had been reached (started, skipped or abandoned).
    dispatched_at: str | None = None
    # The stages in the waves they were scheduled in (``schedule``), for a
    # reader of the matrix. What a stage really waits for is decided again
    # as the gate runs, from what has settled.
    waves: list = dataclasses.field(default_factory=list)
    # One entry per stage, in configuration order: the stage's record
    # (StagePlan.record) plus "group_id", "error" and "outcome".
    stages: list = dataclasses.field(default_factory=list)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    # Whether this gate's completion notice has been started. Not stored: a
    # gate that settled while the controller was down is not mailed on
    # startup, as a run that ended then is not (notify.py), and the ledger
    # beside the run directories is what keeps one that was sent unique.
    notified: bool = False

    def record(self) -> dict:
        return {name: getattr(self, name) for name in _GATE_FIELDS}

    def stage(self, name: str) -> dict | None:
        for entry in self.stages:
            if entry.get("stage") == name:
                return entry
        return None

    def group_ids(self) -> list[str]:
        return [
            entry["group_id"] for entry in self.stages
            if isinstance(entry.get("group_id"), str)
        ]

    def set_stage_group(self, name: str, group_id: str) -> bool:
        entry = self.stage(name)
        if entry is None or entry.get("group_id") == group_id:
            return False
        entry["group_id"] = group_id
        entry["error"] = None
        entry["outcome"] = None
        return True

    def set_stage_outcome(self, name: str, outcome: str, error: str) -> bool:
        """Record a stage the gate decided not to run, and why.

        A stage that has a group keeps it: its own run is the answer, and
        nothing here may overwrite one.
        """
        entry = self.stage(name)
        if entry is None or entry.get("group_id") or entry.get("outcome"):
            return False
        if outcome not in STAGE_OUTCOMES:
            raise ReleaseGateError(f"not a stage outcome: {outcome!r}")
        entry["outcome"] = outcome
        entry["error"] = str(error)[:MAX_REASON_CHARS]
        return True


def new_gate(
    config: str, hosts: Sequence[str], plans: Sequence[StagePlan], *,
    ref: str, commit: str | None, with_bootloader: bool = False,
    waves: Sequence[Sequence[str]] = (),
) -> ReleaseGate:
    """A gate for these compiled stages, with one entry each, in order."""
    return ReleaseGate(
        gate_id=new_gate_id(), config=config, hosts=list(hosts), ref=ref,
        commit=commit, with_bootloader=bool(with_bootloader),
        waves=[list(wave) for wave in waves],
        stages=[
            {**plan.record(), "group_id": None, "error": None, "outcome": None}
            for plan in plans
        ],
    )


def gate_from_record(data: Mapping) -> ReleaseGate:
    """One stored gate record, or ValueError if it cannot be read as one."""
    if not isinstance(data, Mapping):
        raise ValueError("a gate record must be a JSON object")
    if not valid_gate_id(data.get("gate_id")):
        raise ValueError(f"invalid gate_id {data.get('gate_id')!r}")
    gate = ReleaseGate(**{name: data[name] for name in _GATE_FIELDS if name in data})
    if not isinstance(gate.config, str) or not gate.config:
        raise ValueError("config must be a configuration name")
    if not isinstance(gate.hosts, list) or not all(
        isinstance(host, str) for host in gate.hosts
    ):
        raise ValueError("hosts must be a list of host names")
    if not isinstance(gate.created_at, str) or not gate.created_at:
        raise ValueError("created_at must be a timestamp")
    for name in ("dispatched_at", "ref", "commit", "cancel_reason"):
        if not isinstance(getattr(gate, name), (str, type(None))):
            raise ValueError(f"{name} must be a string or null")
    for name in ("with_bootloader", "cancel_requested"):
        if not isinstance(getattr(gate, name), bool):
            raise ValueError(f"{name} must be true or false")
    if not isinstance(gate.stages, list) or not gate.stages or not all(
        isinstance(entry, dict) and isinstance(entry.get("stage"), str)
        for entry in gate.stages
    ):
        raise ValueError("stages must be a non-empty list of stage entries")
    if not isinstance(gate.waves, list) or not all(
        isinstance(wave, list) and all(isinstance(name, str) for name in wave)
        for wave in gate.waves
    ):
        raise ValueError("waves must be a list of lists of stage names")
    names = [entry["stage"] for entry in gate.stages]
    if len(set(names)) != len(names):
        raise ValueError("stages must not name a stage twice")
    for entry in gate.stages:
        for name in ("group_id", "error", "outcome"):
            if not isinstance(entry.get(name), (str, type(None))):
                raise ValueError(f"a stage's {name} must be a string or null")
        if entry.get("outcome") is not None and entry["outcome"] not in STAGE_OUTCOMES:
            raise ValueError(f"not a stage outcome: {entry['outcome']!r}")
        # Every field a dispatch decision or a child request reads, because a
        # record is a file on disk: a stage whose `order` is a word would be
        # loaded, drawn, and then raise where the next stage is chosen, and a
        # `requires` that is a string would be read character by character
        # and quietly lose the requirement -- which is how a flash writer
        # could start before the stage it waits for.
        for name in ("plan", "suite", "bootloader"):
            if not isinstance(entry.get(name), str) or not entry[name]:
                raise ValueError(f"a stage's {name} must be a name")
        for name in ("hosts", "requires", "tests"):
            if not isinstance(entry.get(name), list) or not all(
                isinstance(item, str) for item in entry[name]
            ):
                raise ValueError(f"a stage's {name} must be a list of names")
        if isinstance(entry.get("order"), bool) or not isinstance(
            entry.get("order"), int
        ):
            raise ValueError("a stage's order must be a whole number")
        for name in (
            "optional", "opt_in", "writes_firmware", "build", "flash_build",
            "with_firmware", "with_btbr", "with_python",
        ):
            if not isinstance(entry.get(name), bool):
                raise ValueError(f"a stage's {name} must be true or false")
    groups = gate.group_ids()
    if len(set(groups)) != len(groups):
        raise ValueError("two stages must not name the same run group")
    return gate


# ----- the stage matrix and the verdict ------------------------------------


# Why a stage has no group of its own.
NOT_STARTED = "it was never started"
GROUP_GONE = "its run group is no longer stored"


def _stage_status(entry: Mapping, group: Mapping | None, *, dispatched: bool) -> str:
    """One stage's verdict, from its child group or from its own record.

    A stage with a group reports what that group came to: its children are
    the measurement. A stage with none reports what the gate recorded about
    it -- skipped, abandoned, cancelled, or a start that failed -- and a
    stage the gate has not reached yet is pending.

    A stage whose group *was* recorded and is no longer stored (its runs fell
    outside the retention window) is an error of the gate. Never pending: a
    pending stage is one to start, and this one has already run, on boards a
    writer stage would then write twice. Never ``not_reached`` either, which
    would read as a stage nobody got to.
    """
    if group is not None:
        return str(group.get("status") or ERROR)
    if entry.get("group_id"):
        return ERROR
    outcome = entry.get("outcome")
    if outcome in STAGE_OUTCOMES:
        return outcome
    if entry.get("error"):
        # A start that failed with nothing recorded about it: the stage did
        # not run, and a required stage that did not run is not a pass.
        return SKIPPED if entry.get("optional") else FAILED
    return PENDING if not dispatched else NOT_REACHED


def stage_views(
    gate: ReleaseGate, groups: Mapping[str, Mapping]
) -> list[dict]:
    """One row per stage: what was asked for, what happened, and where it is.

    ``groups`` maps a group ID to that group's own view
    (``rungroups.group_view``), so nothing here reads a run record. Each row
    carries the stage's run IDs, the commit every child was sent, the device
    roles it bound, its start and end, its duration in seconds and in words,
    its verdict, the reason it has one, and the API paths of each child's
    report and records.
    """
    rows = []
    for entry in gate.stages:
        group_id = entry.get("group_id") if isinstance(entry.get("group_id"), str) else None
        group = groups.get(group_id) if group_id else None
        error = entry.get("error")
        if group is None and group_id and not error:
            error = GROUP_GONE
        status = _stage_status(entry, group, dispatched=bool(gate.dispatched_at))
        members = list((group or {}).get("members") or ())
        runs = []
        for member in members:
            run = member.get("run") if isinstance(member, Mapping) else None
            run_id = member.get("run_id") if isinstance(member, Mapping) else None
            runs.append({
                "host": (member or {}).get("host"),
                "run_id": run_id,
                "status": (member or {}).get("status"),
                "error": (member or {}).get("error"),
                "commit": (run or {}).get("commit") or gate.commit,
                "started_at": (run or {}).get("created_at"),
                "finished_at": (run or {}).get("finished_at"),
                "duration_s": (run or {}).get("duration_s"),
                "duration_hms": (run or {}).get("duration_hms"),
                "reason": (run or {}).get("reason"),
                # Where a reader finds the stage's own evidence. The report
                # is fetched from the controller; the records stay in the
                # runner's own run directory, whose path the run records.
                "report": f"/api/runs/{run_id}/report.md" if run_id else None,
                "run_directory": (run or {}).get("runner_run_directory"),
            })
        started = [run["started_at"] for run in runs if run["started_at"]]
        ended = [run["finished_at"] for run in runs if run["finished_at"]]
        finished_at = (
            max(ended) if ended and len(ended) == len(runs) and runs else None
        )
        duration_s = (
            wall_seconds(min(started), finished_at)
            if started and finished_at else None
        )
        reason = ""
        if error:
            reason = str(error)[:MAX_REASON_CHARS]
        elif status in BAD:
            reason = next(
                (run["reason"] for run in runs if run.get("reason")),
                next((run["error"] for run in runs if run.get("error")), ""),
            ) or (
                f"the stage ended {status}"
            )
        rows.append({
            **{
                name: entry.get(name) for name in (
                    "stage", "purpose", "condition", "optional", "opt_in",
                    "writes_firmware", "restores_application_image",
                    "requires", "order", "plan", "suite",
                    "tests", "hosts", "resources", "dropped_tests",
                    "estimated_s", "build", "flash_build", "with_firmware",
                    "with_btbr", "with_python", "bootloader",
                )
            },
            "group_id": group_id,
            "status": status,
            "started_at": min(started) if started else None,
            "finished_at": finished_at,
            "duration_s": duration_s,
            "duration_hms": human_duration_or_none(duration_s),
            "reason": reason,
            "runs": runs,
        })
    return rows


def gate_status(rows: Sequence[Mapping]) -> str:
    """One verdict for the gate: passed only when every required stage passed.

    ``running`` while any stage is still to come or still going; otherwise
    the worst of what the **required** stages came to
    (``rungroups.RANK_ORDER``). A gate with no stage at all is an error: it
    measured nothing.

    An optional stage does not decide the verdict, which is what makes it
    optional: a bench without the fixture it needs would otherwise never
    produce a pass, and its own row and ``optional_problems`` are where a
    reader sees what it came to.
    """
    if not rows:
        return ERROR
    statuses = [str(row.get("status") or ERROR) for row in rows]
    if any(status in UNFINISHED for status in statuses):
        return RUNNING
    required = [
        str(row.get("status") or ERROR) for row in rows if not row.get("optional")
    ]
    if not required:
        return worst(statuses)
    return worst(required)


def pending_decisions(
    rows: Sequence[Mapping], *, busy_hosts: Sequence[str] = ()
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Which stages may start now, and which are abandoned, given the matrix.

    Called every time a child run ends, with the stage matrix as it then
    stands, so the schedule is decided from what has really settled rather
    than replayed from the waves it was laid out in. The rules are the
    waves' own (``schedule``), one stage at a time:

    - a stage whose required stage did not pass is abandoned, with the
      reason naming it. A required stage that was skipped because the
      configuration marks it optional does not abandon anything;
    - a stage whose required stages have not settled waits;
    - a stage waits for every unsettled stage of a lower ``order`` that
      shares a host with it, which is what keeps a flash writer behind the
      stages that need the run's firmware on that board;
    - a stage waits while any stage sharing a host with it is running, and
      while one of its hosts is ``busy_hosts`` (a request the caller has out
      but no child for yet). A target runner runs one run at a time;
    - anything else may start.

    Both answers are in matrix order, so the same situation always produces
    the same dispatch.
    """
    by_name = {str(row.get("stage")): row for row in rows}
    held = {host for host in busy_hosts}
    for row in rows:
        if str(row.get("status")) == RUNNING:
            held.update(str(host) for host in row.get("hosts") or ())
    ready: list[str] = []
    abandoned: list[tuple[str, str]] = []
    for row in rows:
        name = str(row.get("stage"))
        if str(row.get("status")) != PENDING:
            continue
        failed = []
        waiting = False
        for required in row.get("requires") or ():
            other = by_name.get(str(required))
            if other is None:
                continue
            status = str(other.get("status"))
            if status == PASSED:
                continue
            if status == SKIPPED and other.get("optional"):
                continue
            if status in UNFINISHED:
                waiting = True
            else:
                failed.append(f"{required} ({status})")
        if failed:
            abandoned.append((
                name,
                "it was not run because the stage(s) it requires did not "
                "pass: " + ", ".join(failed),
            ))
            continue
        if waiting:
            continue
        hosts = {str(host) for host in row.get("hosts") or ()}
        if hosts & held:
            continue
        if any(
            str(other.get("status")) in UNFINISHED
            and int(other.get("order") or 0) < int(row.get("order") or 0)
            and hosts & {str(host) for host in other.get("hosts") or ()}
            for other in rows
        ):
            continue
        ready.append(name)
        # Its hosts are taken for this round: two stages of one wave may run
        # at once only when their host sets are disjoint.
        held.update(hosts)
    return tuple(ready), tuple(abandoned)


def _restore_verified(row: Mapping, rows: Sequence[Mapping]) -> bool:
    """Did a later stage on this board write and preflight the application image?

    True when a stage of a higher ``order`` that shares a host with this one
    passed: that stage flashed the run's application firmware and its
    preflight verified the identity, which is the check this one's own
    write-back is taken on trust without.
    """
    hosts = {str(host) for host in row.get("hosts") or ()}
    return any(
        str(other.get("status")) == PASSED
        and int(other.get("order") or 0) > int(row.get("order") or 0)
        and hosts & {str(host) for host in other.get("hosts") or ()}
        for other in rows
    )


def gate_view(
    gate: ReleaseGate, groups: Mapping[str, Mapping],
    *, manual_gates: Sequence[Mapping] = MANUAL_GATES,
) -> dict:
    """The gate as its card, its summary and its notice read it."""
    rows = stage_views(gate, groups)
    status = gate_status(rows)
    ended = [row["finished_at"] for row in rows if row["finished_at"]]
    settled = [row for row in rows if str(row["status"]) not in UNFINISHED]
    finished_at = (
        (max(ended) if ended else gate.dispatched_at)
        if status not in UNFINISHED else None
    )
    duration_s = (
        wall_seconds(gate.created_at, finished_at)
        if finished_at and any(row["started_at"] for row in rows) else None
    )
    required = [row for row in rows if not row["optional"]]
    return {
        # A stage that leaves the alternate image on a board writes the
        # application image back itself, and the next writer on that board
        # flashes and preflights it, which is what verifies the restore. When
        # that stage did not run, the restore is the stage's own word for it.
        "unverified_restores": [
            {"stage": row["stage"], "status": row["status"]}
            for row in rows
            if row.get("restores_application_image")
            and not _restore_verified(row, rows)
        ],
        **{name: getattr(gate, name) for name in _GATE_FIELDS if name != "stages"},
        "status": status,
        "active": status == RUNNING,
        "finished_at": finished_at,
        "duration_s": duration_s,
        "duration_hms": human_duration_or_none(duration_s),
        "stages_asked": len(rows),
        "stages_settled": len(settled),
        "stages_passed": sum(row["status"] == PASSED for row in rows),
        "required_asked": len(required),
        "required_passed": sum(row["status"] == PASSED for row in required),
        # The optional stages that did not pass, so a verdict they do not
        # decide still says what became of them. A stage that was skipped is
        # not one of them: it ran nowhere, which is what optional means, and
        # its own row says so.
        "optional_problems": [
            {"stage": row["stage"], "status": row["status"], "reason": row["reason"]}
            for row in rows
            if row["optional"]
            and row["status"] not in (PASSED, SKIPPED, *UNFINISHED)
        ],
        # Every stage's test time added up, which is not the wall estimate
        # preflight reports (that one costs a wave its slowest stage and adds
        # each stage's build): named apart so a reader cannot take one for
        # the other.
        "test_seconds_total": (
            None if any(row["estimated_s"] is None for row in rows)
            else round(sum(row["estimated_s"] for row in rows), 1)
        ),
        "stages": rows,
        # Gates this configuration has no stage for, so a reader of the
        # summary is told rather than left to notice.
        "manual_gates": [dict(entry) for entry in manual_gates],
        "excluded": [dict(entry) for entry in EXCLUDED],
    }
