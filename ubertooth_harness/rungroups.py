"""Run groups: one plan on several hosts, one child run each, one result.

A run group is the controller's record of "run this plan on these hosts". Each
selected host gets its own child run, with its own runner, boards and leases;
the runs are started together and are otherwise ordinary runs, so neither the
target runner protocol nor anything on a target changes. A group of one is
the ordinary single-host run: a run that belongs to no stored group is
presented as a group of one (``implicit_group``), so history has one shape.

The group's status is derived from its children, never stored: ``running``
while any child is active, ``passed`` only when every host the group asked
for ran and passed, and otherwise the worst of what the children came to
(``RANK_ORDER``). A host whose run never started counts as an ``error``, which
is why a group that started only some of its hosts never reads as passed.

``host_steps`` turns one child run's records into the per-host step results a
pill is drawn from (build, flash, preflight, and one step per test), and
``merge_steps`` lays those out as one row per step with one cell per host that
step runs on, and only those hosts. Both read a run's own run.jsonl records,
which stay the source of truth; nothing here reaches a target.
"""

import dataclasses
import datetime
from typing import Collection, Mapping, Sequence
import uuid

# The run records' timestamps are read the way the estimate store reads them,
# so a duration shown here and a duration learned from the same pair of
# records cannot disagree.
from ubertooth_harness.learned import _elapsed, _timestamp
from ubertooth_harness.protocol import _identifier


# Group records live beside the run directories, one file per group.
GROUPS_DIR = "groups"
# A group ID cannot be a run ID: run IDs start with a UTC timestamp.
GROUP_ID_PREFIX = "grp-"
# How many hosts one group may name. Each host is a full run with its own
# boards, so this is about what a bench can hold, not about the controller.
MAX_GROUP_HOSTS = 16

# Step kinds. A test step's key is the test name, so a test that runs once
# per board is one step with one part per invocation.
BUILD_STEP = "build"
FLASH_STEP = "flash"
PREFLIGHT_STEP = "preflight"
TEST_STEP = "test"
# The stage steps, in the order a run reaches them.
STAGE_STEPS = (BUILD_STEP, FLASH_STEP, PREFLIGHT_STEP)
STEP_NAMES = {
    BUILD_STEP: "Build",
    FLASH_STEP: "Flash the boards",
    PREFLIGHT_STEP: "Preflight",
}
# Bounds on what one run's records can turn into. A plan takes at most 100
# tests and a bench has a handful of boards, so these are past any real run
# and keep a record that names something else from growing the answer: the
# records are data, not something this side chose.
# A name longer than this is not shortened but dropped, with the record it
# came in: two invocations whose IDs differ past a cut would otherwise become
# one part, and one of their results would stand for both. It is well past
# any real name (the protocol's own limit on a test name is 200).
MAX_LABEL_CHARS = 256
MAX_REASON_CHARS = 2000
# A status is one word; a record carrying anything longer is not reporting a
# status at all.
MAX_STATUS_CHARS = 64
# A plan takes at most 100 tests (the protocol's ``tests`` limit), and a test
# runs once per selected board on a bench of a handful, so these leave room
# and still bound what one host's records can turn into.
MAX_TEST_STEPS = 150
MAX_STEP_PARTS = 64
# Parts over all of one host's steps, which is what bounds the answer: the
# per-step limits alone would multiply.
MAX_HOST_PARTS = 600

RUNNING = "running"
PENDING = "pending"
# A member whose start request has been sent and not yet answered. Only a
# member status: the group reads as running while one of its hosts is still
# being started.
STARTING = "starting"
# Planned, but the run ended before it was reached.
NOT_REACHED = "not_reached"
PASSED = "passed"
FAILED = "failed"
# A required test the planner could not compile on that host: the hardware it
# needs is not registered there, which fails the run (planner.py, #273).
BLOCKED = "blocked"
CANCELED = "canceled"
ERROR = "error"
LOST = "lost"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"

# Worst first: the status of a step, or of a whole group, is the worst of its
# parts. A status this module does not know ranks with ``error``, because an
# outcome nobody here understands is a problem, not a pass. A run that was
# lost (its runner no longer has it) ranks worse than one that was cancelled,
# which is what somebody asked for. ``not_applicable`` ranks last: a test
# that passed on one board and did not apply on another passed as far as it
# could.
RANK_ORDER = (
    RUNNING, ERROR, FAILED, BLOCKED, LOST, CANCELED, SKIPPED, NOT_REACHED,
    PENDING, PASSED, NOT_APPLICABLE,
)
RANK = {status: index for index, status in enumerate(RANK_ORDER)}
# A status a run or step has not reached an answer for.
UNFINISHED = (PENDING, RUNNING)


def new_group_id() -> str:
    return GROUP_ID_PREFIX + uuid.uuid4().hex


def valid_group_id(value) -> bool:
    return _identifier(value)


def rank(status) -> int:
    """How bad a status is; an unknown status ranks with ``error``."""
    return RANK.get(status, RANK[ERROR])


def worst(statuses: Sequence[str]) -> str:
    """The worst of several statuses, as the status itself."""
    return min(statuses, key=rank)


def combine(statuses: Sequence[str]) -> str:
    """One status for the parts of one step on one host.

    A step some of whose parts have started while others have not yet reads
    as running: the first board's invocation passing does not finish a test
    that still has a second board to go.
    """
    if not statuses:
        return PENDING
    outcome = worst(statuses)
    if outcome == PENDING and any(status != PENDING for status in statuses):
        return RUNNING
    return outcome


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _seconds(started_at, finished_at) -> float | None:
    """Wall seconds between two run-record timestamps, or None."""
    begin, end = _timestamp(started_at), _timestamp(finished_at)
    if begin is None:
        return None
    seconds = _elapsed(begin, end)
    return round(seconds, 3) if seconds is not None else None


def _status(value) -> str:
    """One status out of a record; anything unusable is an error.

    Bounded like the other text taken from records: a status is one word, and
    it ends up in every part, step and row of what the API returns.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_STATUS_CHARS:
        return ERROR
    return value


def _reason(details: Mapping) -> str:
    reason = details.get("reason")
    return str(reason)[:MAX_REASON_CHARS] if reason else ""


def _label(value) -> str:
    """A name out of a run record: a test, an invocation, or a board.

    Empty for anything that is not a usable name, including one longer than
    ``MAX_LABEL_CHARS``, and the record it came in is then left out: every
    one of these becomes a step key or a part of one in what the API
    returns, and a record is data (a runner's protocol message may be
    megabytes). Dropping such a name is safer than shortening it, which
    would merge two invocations that differ past the cut.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_LABEL_CHARS:
        return ""
    return value


# ----- the group record ----------------------------------------------------


# What a group file holds. Everything else about a group (its status, when it
# finished, what each host did) is derived from its children's own records, so
# no state machine has to be kept in step with them.
_GROUP_FIELDS = (
    "group_id", "hosts", "ref", "commit", "plan", "suite", "tests", "build",
    "flash_build", "with_firmware", "with_python", "created_at",
    "dispatched_at", "members", "cancel_requested", "cancel_reason",
)


@dataclasses.dataclass
class RunGroup:
    """One plan, the hosts it was asked to run on, and each host's child run."""

    group_id: str
    hosts: list = dataclasses.field(default_factory=list)
    ref: str | None = None
    commit: str | None = None
    plan: str | None = None
    suite: str | None = None
    tests: list = dataclasses.field(default_factory=list)
    build: bool = False
    flash_build: bool = False
    with_firmware: bool = False
    with_python: bool = False
    created_at: str = dataclasses.field(default_factory=_now)
    # When the last start request had been answered (or had failed).
    dispatched_at: str | None = None
    # One entry per host asked for, in that order: {"host", "run_id", "error"}.
    members: list = dataclasses.field(default_factory=list)
    cancel_requested: bool = False
    cancel_reason: str | None = None
    # A group of one derived from a run that belongs to no stored group
    # (a single-host run, or a run adopted from a runner report). Not stored.
    implicit: bool = False

    def record(self) -> dict:
        return {name: getattr(self, name) for name in _GROUP_FIELDS}

    def member(self, host: str) -> dict | None:
        for entry in self.members:
            if entry.get("host") == host:
                return entry
        return None

    def run_ids(self) -> list[str]:
        return [
            entry["run_id"] for entry in self.members
            if isinstance(entry.get("run_id"), str)
        ]

    def set_member_run(self, host: str, run_id: str) -> bool:
        """Record the run a host's start request created; True if it changed."""
        entry = self.member(host)
        if entry is None or entry.get("run_id") == run_id:
            return False
        entry["run_id"] = run_id
        entry["error"] = None
        return True

    def set_member_error(self, host: str, error: str) -> bool:
        """Record why a host got no run; a host that has one keeps it."""
        entry = self.member(host)
        if entry is None or entry.get("run_id"):
            return False
        entry["error"] = str(error)[:MAX_REASON_CHARS]
        return True


def group_from_record(data: Mapping) -> RunGroup:
    """One stored group record, or ValueError if it cannot be read as one."""
    if not isinstance(data, Mapping):
        raise ValueError("a group record must be a JSON object")
    if not valid_group_id(data.get("group_id")):
        raise ValueError(f"invalid group_id {data.get('group_id')!r}")
    values = {name: data[name] for name in _GROUP_FIELDS if name in data}
    group = RunGroup(**values)
    if not isinstance(group.hosts, list) or not all(
        isinstance(host, str) for host in group.hosts
    ):
        raise ValueError("hosts must be a list of host names")
    if len(set(group.hosts)) != len(group.hosts):
        # As a group may not be started: one entry per host, or a host's run
        # could never be recorded against its own entry.
        raise ValueError("hosts must not name a target twice")
    if not isinstance(group.members, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("host"), str)
        for entry in group.members
    ):
        raise ValueError("members must be a list of {host, run_id, error}")
    if [entry["host"] for entry in group.members] != group.hosts:
        # One entry per host asked for, in that order: a record whose two
        # lists disagree would report one count as the other.
        raise ValueError("members must be the hosts, in the order asked for")
    if not isinstance(group.tests, list) or not all(
        isinstance(name, str) for name in group.tests
    ):
        raise ValueError("tests must be a list of test names")
    # Every other field as well: a record is a file on disk, and one whose
    # created_at is a number would be read as a group and then break the
    # history list it is sorted into.
    if not isinstance(group.created_at, str) or not group.created_at:
        raise ValueError("created_at must be a timestamp")
    for name in ("dispatched_at", "ref", "commit", "plan", "suite", "cancel_reason"):
        if not isinstance(getattr(group, name), (str, type(None))):
            raise ValueError(f"{name} must be a string or null")
    for name in (
        "build", "flash_build", "with_firmware", "with_python", "cancel_requested"
    ):
        if not isinstance(getattr(group, name), bool):
            raise ValueError(f"{name} must be true or false")
    for entry in group.members:
        for name in ("run_id", "error"):
            if not isinstance(entry.get(name), (str, type(None))):
                raise ValueError(f"a member's {name} must be a string or null")
    run_ids = group.run_ids()
    if len(set(run_ids)) != len(run_ids):
        # One run belongs to one host, or it would be counted once per
        # member and a group of two could read as complete with one child.
        raise ValueError("two members must not name the same run")
    return group


def new_group(hosts: Sequence[str], fields: Mapping) -> RunGroup:
    """A group for ``hosts``, with one member entry each, in that order."""
    return RunGroup(
        group_id=new_group_id(),
        hosts=list(hosts),
        ref=fields.get("ref"),
        commit=fields.get("commit"),
        plan=fields.get("plan"),
        suite=fields.get("suite"),
        tests=list(fields.get("tests") or ()),
        build=bool(fields.get("build")),
        flash_build=bool(fields.get("flash_build")),
        with_firmware=bool(fields.get("with_firmware")),
        with_python=bool(fields.get("with_python")),
        members=[{"host": host, "run_id": None, "error": None} for host in hosts],
    )


def implicit_group(run: Mapping) -> RunGroup:
    """The group of one a run that belongs to no stored group is shown as.

    Its ID is the run's own, so a client that follows a group never has to
    ask whether the run it is looking at was started on its own. Nothing of
    it is stored: every field comes from the run's record.
    """
    run_id = run["run_id"]
    host = run.get("target") or ""
    return RunGroup(
        group_id=run_id,
        hosts=[host],
        ref=run.get("ref"),
        commit=run.get("commit"),
        plan=run.get("plan"),
        suite=run.get("suite"),
        tests=list(run.get("tests") or ()),
        build=bool(run.get("build")),
        flash_build=bool(run.get("flash_build")),
        with_firmware=bool(run.get("with_firmware")),
        with_python=bool(run.get("with_python")),
        created_at=run.get("created_at") or _now(),
        dispatched_at=run.get("created_at"),
        members=[{"host": host, "run_id": run_id, "error": None}],
        cancel_requested=bool(run.get("cancel_requested")),
        implicit=True,
    )


# ----- group status and views ----------------------------------------------


# Why a member has no run status of its own.
NO_RUN = "its run was never started"
RUN_GONE = "its run is no longer stored"


def member_views(
    group: RunGroup, runs: Mapping[str, Mapping], pending: Collection[str] = ()
) -> list[dict]:
    """One entry per host the group asked for, with that host's run.

    ``runs`` maps run ID to the run's own view. A host with no run, or whose
    run is no longer stored, is an ``error`` of the group: it did not do what
    the group asked for. A host that has neither a run nor a reason is
    ``starting`` while its start request is outstanding (``pending``) or
    before the children have all been asked for (``dispatched_at``): nothing
    has gone wrong with it yet, and a group whose child may still arrive must
    not read as finished and then run again.
    """
    views = []
    for entry in group.members:
        run_id = entry.get("run_id") if isinstance(entry.get("run_id"), str) else None
        run = runs.get(run_id) if run_id else None
        error = entry.get("error")
        starting = (
            run is None and not run_id and not error
            and (not group.dispatched_at or entry.get("host") in pending)
        )
        if run is None and not starting:
            error = error or (RUN_GONE if run_id else NO_RUN)
        views.append({
            "host": entry.get("host"),
            "run_id": run_id,
            "error": None if run is not None or starting else error,
            "status": (
                run["status"] if run is not None
                else STARTING if starting else ERROR
            ),
            "run": dict(run) if run is not None else None,
        })
    return views


def group_status(members: Sequence[Mapping]) -> str:
    """``running`` while any child is active, else the worst child outcome.

    A host still being started counts as running: the group is under way,
    and only once every request has been answered can a host that got no run
    be an error of the group.
    """
    if not members:
        return ERROR
    return worst([
        RUNNING if _status(member.get("status")) == STARTING
        else _status(member.get("status"))
        for member in members
    ])


def _finished_at(group: RunGroup, members: Sequence[Mapping]) -> str | None:
    """When the last child ended, or when the group gave up starting them."""
    moments = [
        member["run"]["finished_at"] for member in members
        if member.get("run") and member["run"].get("finished_at")
    ]
    if moments:
        return max(moments)
    return group.dispatched_at


def group_view(
    group: RunGroup, runs: Mapping[str, Mapping], pending: Collection[str] = ()
) -> dict:
    """The group as the history list and the run card read it.

    ``pending`` names the hosts whose start request is still outstanding
    (see ``member_views``).
    """
    members = member_views(group, runs, pending)
    status = group_status(members)
    started = [member for member in members if member["run"] is not None]
    return {
        **{
            name: getattr(group, name) for name in _GROUP_FIELDS
            if name not in ("members",)
        },
        "implicit": group.implicit,
        "status": status,
        "finished_at": None if status == RUNNING else _finished_at(group, members),
        "hosts_asked": len(group.members),
        "hosts_started": len(started),
        # True when every host the group asked for got a run: a group that
        # started only some of its hosts is partly started, and says so here
        # as well as in each member's own error.
        "complete": len(started) == len(group.members) and bool(group.members),
        "active": status == RUNNING,
        "members": members,
    }


# ----- per-host step results -----------------------------------------------


# Which step a stage record belongs to, and whether it opens it.
_STAGE_EVENTS = {
    "build_started": (BUILD_STEP, True),
    "build_finished": (BUILD_STEP, False),
    "build_flash_started": (FLASH_STEP, True),
    "build_flash_finished": (FLASH_STEP, False),
    "preflight_started": (PREFLIGHT_STEP, True),
    "preflight_finished": (PREFLIGHT_STEP, False),
}


def _part(identifier: str, name: str) -> dict:
    """One part of a step on one host: a test invocation, or a flashed board."""
    return {
        "id": identifier, "name": name, "status": PENDING, "started_at": None,
        "finished_at": None, "elapsed_s": None, "reason": "", "estimated_s": None,
    }


def _open(part: dict, timestamp) -> None:
    part["status"] = RUNNING
    part["started_at"] = timestamp if isinstance(timestamp, str) else None
    # A step that starts again (a second test_started for one invocation)
    # is running, and nothing about it has finished.
    part["finished_at"] = None
    part["elapsed_s"] = None
    part["reason"] = ""


def _close(part: dict, status: str, timestamp, reason: str = "") -> None:
    part["status"] = status
    part["finished_at"] = timestamp if isinstance(timestamp, str) else None
    part["reason"] = reason
    part["elapsed_s"] = _seconds(part["started_at"], part["finished_at"])


def _planned_tests(invocations: Sequence[Mapping] | None) -> dict[str, list[dict]]:
    """The run's planned test invocations per test name, in planned order.

    From the run's manifest record (``gui.planned_invocations``), which is
    what the planner compiled for that host: a test the host was left out of
    is not there, which is how a Linux-only test ends up with a Linux pill
    and no macOS one.
    """
    planned: dict[str, list[dict]] = {}
    total = 0
    for invocation in invocations or ():
        if not isinstance(invocation, Mapping):
            continue
        name = _label(invocation.get("test_name"))
        identifier = _label(invocation.get("invocation_id"))
        if not name or not identifier:
            continue
        if name not in planned and len(planned) >= MAX_TEST_STEPS:
            continue
        if len(planned.get(name, ())) >= MAX_STEP_PARTS or total >= MAX_HOST_PARTS:
            continue
        total += 1
        part = _part(identifier, name)
        part["estimated_s"] = invocation.get("estimated_s")
        planned.setdefault(name, []).append(part)
    return planned


def _stage_estimate(stages: Sequence[Mapping], step: str) -> float | None:
    """What this host has learned the build or preflight stage takes.

    Only those two: a flash of the run's build is not learned as a stage (the
    estimate store learns flashing from the Flash reliability runner, which
    times its own flashes), so that pill has no estimate to fill from.
    """
    for stage in stages or ():
        if isinstance(stage, Mapping) and stage.get("stage") == step:
            return stage.get("estimated_s")
    return None


def host_steps(
    events: Sequence[Mapping],
    *,
    build: bool = False,
    flash_build: bool = False,
    run_status: str = RUNNING,
    invocations: Sequence[Mapping] | None = None,
    stages: Sequence[Mapping] = (),
) -> list[dict]:
    """One run's build, flash, preflight and test steps, in the order it runs them.

    Each step carries its status, when it started and finished, how long it
    took, why it failed, what it is estimated to take, and its parts: a test
    step's invocations (one per board for a per-board test), a flash step's
    boards. A test step's status is the worst of its invocations; a stage
    step's is its own record's, because a flash that failed after every board
    reported is still a failed flash.

    ``invocations`` is the run's planned invocations and ``build``/
    ``flash_build`` what it was asked for, so a step the run has not reached
    yet is listed as pending rather than left out. Once the run is over, a
    pending step is ``not_reached``: the run ended before it.
    """
    stage_parts: dict[str, dict] = {}
    boards: dict[str, dict] = {}
    tests = _planned_tests(invocations)
    # Parts by invocation ID, so a record finds the part it opened even when
    # the manifest did not plan it (an adopted run has no manifest here).
    by_invocation = {
        part["id"]: part for parts in tests.values() for part in parts
    }
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        kind = event.get("event_type")
        timestamp = event.get("timestamp")
        status = _status(event.get("status"))
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        stage = _STAGE_EVENTS.get(kind)
        if stage is not None:
            step, opening = stage
            part = stage_parts.setdefault(step, _part(step, STEP_NAMES[step]))
            if opening:
                _open(part, timestamp)
            else:
                _close(part, status, timestamp, _reason(details))
        elif kind in ("build_flash_board_started", "build_flash_board_finished"):
            resource = _label(details.get("resource"))
            if not resource or (
                resource not in boards and len(boards) >= MAX_STEP_PARTS
            ):
                continue
            part = boards.setdefault(resource, _part(resource, resource))
            if kind.endswith("_started"):
                _open(part, timestamp)
            else:
                _close(part, status, timestamp, _reason(details))
        elif kind == "blocked":
            # A required test this host's run could not run: the hardware it
            # is about is not registered here, or not anywhere in this
            # registry. It is in no invocation, so the record is the step:
            # without one the host would show nothing for a test that fails
            # its run.
            name = _label(details.get("test_name"))
            if not name or (name not in tests and len(tests) >= MAX_TEST_STEPS):
                continue
            parts = tests.get(name, [])
            if any(part["id"] == name for part in parts):
                continue
            # Checked before the step is added, so a record at a bound
            # cannot leave a step with no part at all, which would read as a
            # test nobody reached.
            if len(parts) >= MAX_STEP_PARTS or len(by_invocation) >= MAX_HOST_PARTS:
                continue
            parts = tests.setdefault(name, parts)
            part = _part(name, name)
            _close(part, BLOCKED, timestamp, _reason(details))
            by_invocation.setdefault(name, part)
            parts.append(part)
        elif kind in ("test_started", "test_finished"):
            name = _label(details.get("test_name"))
            identifier = _label(details.get("invocation_id"))
            if not name or not identifier:
                continue
            part = by_invocation.get(identifier)
            if part is None:
                # An invocation the manifest did not plan (a run adopted from
                # a runner report has no manifest here). Bounded, so records
                # that name one test after another cannot grow the answer.
                parts = tests.get(name)
                if parts is None and len(tests) >= MAX_TEST_STEPS:
                    continue
                if (parts is not None and len(parts) >= MAX_STEP_PARTS) or (
                    len(by_invocation) >= MAX_HOST_PARTS
                ):
                    continue
                part = _part(identifier, name)
                by_invocation[identifier] = part
                tests.setdefault(name, []).append(part)
            if kind == "test_started":
                _open(part, timestamp)
            else:
                _close(part, status, timestamp, _reason(details))
    steps = []
    for step in STAGE_STEPS:
        part = stage_parts.get(step)
        if part is None:
            # Preflight is mandatory, so every run has one; the other two are
            # listed only when the run was asked for them.
            if step == PREFLIGHT_STEP or (step == BUILD_STEP and build) or (
                step == FLASH_STEP and build and flash_build
            ):
                part = _part(step, STEP_NAMES[step])
            else:
                continue
        steps.append({
            "kind": step, "key": step, "name": STEP_NAMES[step],
            "status": part["status"], "started_at": part["started_at"],
            "finished_at": part["finished_at"], "elapsed_s": part["elapsed_s"],
            "reason": part["reason"],
            "estimated_s": _stage_estimate(stages, step),
            "items": list(boards.values()) if step == FLASH_STEP else [],
        })
    for name, parts in tests.items():
        seconds = [part["estimated_s"] for part in parts]
        started = [part["started_at"] for part in parts if part["started_at"]]
        ended = [part["finished_at"] for part in parts if part["finished_at"]]
        finished_at = max(ended) if ended and len(ended) == len(parts) else None
        steps.append({
            "kind": TEST_STEP, "key": name, "name": name,
            "status": combine([part["status"] for part in parts]),
            "started_at": min(started) if started else None,
            "finished_at": finished_at,
            "elapsed_s": _seconds(min(started), finished_at) if started else None,
            "reason": next((part["reason"] for part in parts if part["reason"]), ""),
            "estimated_s": (
                round(sum(seconds), 1) if seconds and None not in seconds else None
            ),
            "items": parts,
        })
    if run_status not in UNFINISHED:
        _settle(steps, run_status)
    return steps


def _settle(steps: Sequence[dict], run_status: str) -> None:
    """Close the steps of a run that has ended.

    A part the run never reached is ``not_reached``; a part still open when
    the run ended takes the run's own outcome (a cancel during a test is why
    that test has no result), except that a passed run cannot have left one
    open, so such a part is an error of the records.

    A test step is then the worst of its settled parts, so an invocation that
    failed before the run was cancelled is what its host's pill shows.
    """
    open_status = ERROR if run_status == PASSED else run_status

    def settle(part: dict) -> None:
        if part["status"] == PENDING:
            part["status"] = NOT_REACHED
        elif part["status"] == RUNNING:
            part["status"] = open_status

    for step in steps:
        for part in step["items"]:
            settle(part)
        if step["kind"] == TEST_STEP and step["items"]:
            step["status"] = combine([part["status"] for part in step["items"]])
        else:
            settle(step)


# ----- the group's steps ---------------------------------------------------


_CELL_FIELDS = (
    "status", "started_at", "finished_at", "elapsed_s", "reason",
    "estimated_s", "items",
)


def merge_steps(
    per_host: Mapping[str, Sequence[Mapping]], order: Sequence[str] = ()
) -> list[dict]:
    """One row per step, with one cell per host that step runs on, and only those.

    Rows are the stage steps in the order a run reaches them, then the tests
    in planned order: the order of the first host that plans each test, with
    a test only another host plans after the tests it shares. A host that
    does not run a step has no cell in that row, which is what keeps a
    Linux-only test from showing a macOS pill.
    """
    hosts = list(order) + [host for host in per_host if host not in order]
    rows: dict[tuple[str, str], dict] = {}
    for host in hosts:
        for step in per_host.get(host) or ():
            key = (step["kind"], step["key"])
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "kind": step["kind"], "key": step["key"], "name": step["name"],
                    "order": len(rows), "hosts": {},
                }
            row["hosts"][host] = {
                "host": host, **{name: step[name] for name in _CELL_FIELDS}
            }
    def place(row: dict) -> tuple:
        if row["kind"] in STAGE_STEPS:
            return (0, STAGE_STEPS.index(row["kind"]), 0)
        return (1, 0, row["order"])
    merged = []
    for row in sorted(rows.values(), key=place):
        cells = list(row["hosts"].values())
        merged.append({
            "kind": row["kind"], "key": row["key"], "name": row["name"],
            "status": combine([cell["status"] for cell in cells]),
            "hosts": row["hosts"],
        })
    return merged


def group_progress(steps: Sequence[Mapping]) -> dict:
    """How far the group is: per-host steps done, and the time they should take.

    One unit per cell, so a group of two hosts counts each host's preflight.
    A step the run never reached counts as done: nothing more will happen to
    it. ``estimated_s`` is the sum over every cell, and None unless every one
    of them has an estimate, exactly as a suite total is; a group that
    flashes its boards therefore has none, since flashing the run's build is
    not a learned stage (``_stage_estimate``).
    """
    cells = [cell for step in steps for cell in step["hosts"].values()]
    seconds = [cell["estimated_s"] for cell in cells]
    return {
        "planned": len(cells),
        "completed": sum(cell["status"] not in UNFINISHED for cell in cells),
        "estimated_s": (
            round(sum(seconds), 1) if seconds and None not in seconds else None
        ),
    }
