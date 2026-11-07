"""The public-safe validation evidence one release goes out with (#370).

A release of the Ubertooth fork is qualified on this bench, and the records
that qualified it are written for the bench: absolute paths, the lease
holder's user and machine name, the registry's aliases, every device serial
(``docs/reference/run-records.md``). This module turns a fixed set of finished
run directories into three assets that may be attached to a public release:

* ``validation-summary.md`` -- the release and the exact commits, which gates
  were asked for and which were met, every run with its platform class, UTC
  start and end, totals and verdict, the measurements the runs recorded, every
  deviation, and the names of the logs that travel with it;
* ``validation-logs.tar.zst`` -- each run's ``report.md``, its ``run.jsonl``,
  and the command output files it saved, each sanitized, with
  ``MANIFEST-SHA256`` naming the SHA-256 of every member;
* ``validation-SHA256SUMS`` -- the SHA-256 of the two assets above, in the
  format ``sha256sum -c`` reads.

Three rules hold the whole thing together.

**Nothing private leaves by construction, and nothing private leaves by
accident either.** Every byte is rewritten by ``sanitize.py`` and then read
again by its scan, which fails closed: a bundle is written only once every
member and the summary itself come back clean. The names to remove are read
off the runs' own records rather than typed in, so a bench that registers its
host under its host name is covered like any other.

**Only text the run recorded travels.** A member is a file the run's own
records name, and only the text ones: a capture is third-party traffic and
stays on the test host, listed in the summary with its size and SHA-256 so a
reader knows it exists and can ask for it.

**A gap is reported, never dropped.** Which gates a phase requires is in
:data:`GATES`; a required gate with no run behind it is listed in the summary
under its own heading, and the command exits nonzero unless the operator says
in so many words that an incomplete bundle is what they want.

Nothing here uploads anything. :func:`upload_command` writes out the exact
``gh release upload`` line for the release owner to run after reading the
summary, which is the one step that must not be automatic: publishing is the
decision, and this only prepares it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import datetime
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

from ubertooth_harness import release as release_version
from ubertooth_harness import sanitize
from ubertooth_harness.events import RUN_LOG, REPORT
from ubertooth_harness.outputfiles import (
    OutsideRun, confined, output_records, saved_output,
)
from ubertooth_harness.report import load_events, serials as run_serials
from ubertooth_harness.sanitize import Private


class EvidenceError(ValueError):
    """The export cannot be built from what it was given."""


# The three assets, named as docs/release-testing.md section 10 names them.
SUMMARY_NAME = "validation-summary.md"
LOGS_NAME = "validation-logs.tar.zst"
SUMS_NAME = "validation-SHA256SUMS"
# The SHA-256 of every member, inside the archive. It cannot list itself, and
# the archive's own hash is in validation-SHA256SUMS.
MANIFEST_NAME = "MANIFEST-SHA256"
# The private bundle's names, so the two cannot be confused once they are
# both on somebody's disk. A private bundle keeps the device serials
# (``--private``) and gets no upload command.
PRIVATE_SUMMARY_NAME = "validation-summary-private.md"
PRIVATE_LOGS_NAME = "validation-logs-private.tar.zst"
PRIVATE_SUMS_NAME = "validation-private-SHA256SUMS"

# zstandard's level. Fixed, because the archive has to come out the same
# bytes from the same runs: a reader who rebuilds the bundle from the
# retained records must get the hash the release published.
COMPRESSION_LEVEL = 10
# What a member may be. A run saves text logs, captures (.pcap, .pcapng),
# and binary dumps; only text travels. A capture holds whatever third-party
# traffic was on the air, which is nobody's to publish, and a dump cannot be
# sanitized at all: there is no line in it to rewrite.
TEXT_SUFFIXES = (".log", ".txt", ".md", ".jsonl")
# Why a recorded file stays behind, for the summary's own table.
NOT_TEXT = "binary; a capture or a dump is not published"
MISSING = "the run no longer has it"

# The repository a release of the fork is published from. The bundle is
# attached to that release, not to the harness's own.
FORK_REPOSITORY = "SqueakyFloors/ubertooth"

# One git question, one short read of local history.
GIT_TIMEOUT_S = 20.0


# ---------------------------------------------------------------------------
# Gates: what a phase requires
# ---------------------------------------------------------------------------

SNAPSHOT = "snapshot"
RC = "rc"
RELEASE = "release"
PHASES = (SNAPSHOT, RC, RELEASE)
PHASE_TITLES = {
    SNAPSHOT: "development snapshot",
    RC: "release candidate",
    RELEASE: "final release",
}


@dataclass(frozen=True)
class Gate:
    """One group of evidence a release is judged on.

    ``suites`` are the plan suites whose run satisfies it, which is how a run
    is grouped: a suite is a public name and a run records its own, so
    nothing has to be declared per run. ``by_hand`` marks a gate no suite
    covers -- the installer and the bootloader acceptance of
    ``docs/release-testing.md`` are run by a person -- which is satisfied
    either by a run that recorded it or by an attestation the operator passes
    in (``--attest``), quoted in the summary and marked as attested rather
    than measured.

    ``required_in`` is the phases that require it. A soak is in no phase: no
    soak is a gate (release-testing.md section 11), so a soak that was run is
    reported and a soak that was not is not missing.
    """

    key: str
    title: str
    suites: tuple[str, ...] = ()
    required_in: tuple[str, ...] = ()
    by_hand: bool = False

    def required_for(self, phase: str) -> bool:
        return phase in self.required_in


# The grouping #370 asks for, with the suite names testplans/ actually
# defines. A suite this table does not name is grouped under OTHER_GATE: it
# is evidence and it is reported, it just answers no gate of its own.
GATES = (
    Gate("fast", "Fast gate", ("fast",), (SNAPSHOT, RC, RELEASE)),
    Gate("release", "Canonical non-soak release suite", ("release",), (RC, RELEASE)),
    Gate("alternate-image", "Alternate `btbr` image",
         ("btbr", "btbr-comprehensive", "btbr-long"), (RC, RELEASE)),
    Gate("ble", "Focused BLE suites",
         ("ble", "ble-comprehensive", "connection"), (RC, RELEASE)),
    Gate("classic", "Focused Classic suites",
         ("classic", "classic-comprehensive", "classic-long"), (RC, RELEASE)),
    Gate("hci", "Focused HCI suites", ("hci", "hci-comprehensive"), (RC, RELEASE)),
    Gate("weak-link", "Weak-link checks",
         ("ble-weak-link", "connection-weak-link", "classic-weak-link",
          "range-weak-link"), (RC, RELEASE)),
    Gate("firmware", "Firmware and DFU acceptance",
         ("firmware", "slow"), (RC, RELEASE)),
    Gate("installer", "Installer acceptance", (), (RELEASE,), by_hand=True),
    Gate("bootloader", "Bootloader acceptance", (), (RELEASE,), by_hand=True),
    Gate("soak-4h", "Staged 4-hour soak", ("address-soak-4h",)),
    Gate("soak-24h", "Staged 24-hour soak", ("address-soak-24h",)),
    Gate("soak-72h", "Staged 72-hour soak", ("address-soak-72h",)),
    Gate("other", "Other runs"),
)
OTHER_GATE = "other"
BOOTLOADER_GATE = "bootloader"
GATE_BY_KEY = {gate.key: gate for gate in GATES}
# Which bootloader modes count as acceptance: a run that only recorded what
# each board has compared nothing, so it answers no gate.
BOOTLOADER_ACCEPTANCE_MODES = ("check", "update")


def gate_of(suite: str) -> str:
    """The gate a suite's run belongs to; ``other`` when no gate names it."""
    for gate in GATES:
        if suite in gate.suites:
            return gate.key
    return OTHER_GATE


def phase_of_release(name: str) -> str:
    """The phase a release name implies: DEV, RC or R (release.py's forms).

    A name in none of those forms is a snapshot, which requires the least:
    the operator names the phase with ``--phase`` where that is wrong, and
    the summary says which phase it was judged against either way.
    """
    if not release_version.RELEASE_TAG_FORM.match(name or ""):
        return SNAPSHOT
    if "-RC" in name:
        return RC
    if "-DEV" in name:
        return SNAPSHOT
    return RELEASE


# ---------------------------------------------------------------------------
# Reading one run
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Measurement:
    """One value a run read out of a command's output and recorded."""

    test: str
    name: str
    value: str


@dataclass(frozen=True)
class RecordedFile:
    """One file a run saved, as the run recorded it."""

    path: str
    stream: str
    bytes: int
    lines: int
    sha256: str
    test: str

    @property
    def is_text(self) -> bool:
        return PurePosixPath(self.path).suffix in TEXT_SUFFIXES


@dataclass(frozen=True)
class RunEvidence:
    """What one finished run contributes, read from its own records.

    Everything here comes out of ``run.jsonl``: nothing is inferred and
    nothing is read from the bench. ``private`` is the set of names that run
    requires taking out, which an export merges over its runs so a board one
    run names is masked in another's transcript too.
    """

    directory: Path
    run_id: str
    plan: str
    suite: str
    gate: str
    host_alias: str
    platform: str
    status: str
    started: str
    ended: str
    duration_s: float
    sources: tuple[Mapping, ...]
    # Every (role, alias) the manifest binds, in the order it records them:
    # what :func:`public_labels` turns into the labels the bundle reads by.
    resources: tuple[tuple[str, str], ...]
    requested_tests: int
    completed_tests: int
    failures: tuple[tuple[str, str], ...]
    blocked: tuple[tuple[str, str], ...]
    not_applicable: tuple[tuple[str, str], ...]
    skipped_cases: int
    watchdog_resets: int
    bootloader_mode: str
    failure_reason: str
    measurements: tuple[Measurement, ...]
    files: tuple[RecordedFile, ...]
    private: Private

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def source(self, name: str) -> Mapping:
        for item in self.sources:
            if item.get("name") == name:
                return item
        return {}


def _details(events: Sequence[Mapping], event_type: str) -> dict:
    for event in events:
        if event.get("event_type") == event_type:
            return dict(event.get("details") or {})
    return {}


def _last(events: Sequence[Mapping], event_type: str) -> Mapping | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _platform(manifest: Mapping) -> str:
    """The host's platform class: the system, its version, its architecture.

    Not the kernel build and not the machine: what a reader needs is which
    platform the result was measured on.
    """
    os_info = (manifest.get("environment") or {}).get("os") or {}
    system = str(os_info.get("system") or "").strip()
    version = str(
        os_info.get("macos_version") or os_info.get("distribution") or ""
    ).strip()
    architecture = str(os_info.get("architecture") or "").strip()
    inside = ", ".join(part for part in (version, architecture) if part)
    if system and inside:
        return f"{system} ({inside})"
    return system or inside or "not recorded"


# How deep a record is walked for the holder fields. A lease holder record
# sits one or two levels down (leases.py); the bound is here because the
# records are data and a malformed one must not cost the export its time.
_WALK_DEPTH = 6
# The fields a lease record carries the operator and the machine in
# (leases.holder_info). ``owner`` defaults to the user name.
_USER_KEYS = ("user", "owner")
_HOST_KEYS = ("hostname",)


def _holders(value, depth: int = 0) -> tuple[set[str], set[str]]:
    """Every user and machine name a run's records carry, found by key.

    A run that waited for a board, took a stale lease over, or recorded its
    own leases carries the holder's ``user`` and ``hostname`` -- the
    operator and the lab machine, by name. They are the two things this
    repository's rules are most explicit about, and they arrive in a record
    nobody typed, so they are collected rather than asked for.
    """
    users: set[str] = set()
    hosts: set[str] = set()
    if depth > _WALK_DEPTH:
        return users, hosts
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str) and item.strip():
                if key in _USER_KEYS:
                    users.add(item.strip())
                elif key in _HOST_KEYS:
                    hosts.add(item.strip())
            else:
                found_users, found_hosts = _holders(item, depth + 1)
                users |= found_users
                hosts |= found_hosts
    elif isinstance(value, (list, tuple)):
        for item in value:
            found_users, found_hosts = _holders(item, depth + 1)
            users |= found_users
            hosts |= found_hosts
    return users, hosts


def _measurements(events: Sequence[Mapping]) -> tuple[Measurement, ...]:
    """Every value a run read out of output and recorded, with its test.

    These are the ``observed`` output checks (``outputfiles.OutputCheck``):
    a setting a command printed back, a rate a capture reached, a count a
    case read. A check of another kind recorded a verdict rather than a
    value, and the verdicts are in the results table.
    """
    found: list[Measurement] = []
    for event in events:
        if event.get("event_type") != "output_checks":
            continue
        details = event.get("details") or {}
        test = str(details.get("invocation_id") or details.get("phase") or "run")
        for value in details.get("checks") or []:
            if not isinstance(value, Mapping):
                continue
            if value.get("kind") != "observed":
                continue
            reading = str(value.get("value") or value.get("matched") or "")
            if reading:
                found.append(
                    Measurement(test, str(value.get("name") or ""), reading))
    return tuple(found)


def _files(run_log: Path, events: Sequence[Mapping]) -> tuple[RecordedFile, ...]:
    """The files a run recorded, read a line at a time from its run.jsonl.

    A soak's run.jsonl is tens of megabytes and this list is a few
    kilobytes, so it is read through ``output_records`` rather than off the
    events already in memory where the log is there to read.
    """
    records = output_records(run_log) if run_log.is_file() else events
    found = []
    for item in saved_output(records):
        file = item.file
        found.append(RecordedFile(
            path=file.path, stream=file.stream, bytes=file.bytes,
            lines=file.lines, sha256=file.sha256, test=item.group,
        ))
    return tuple(sorted(found, key=lambda item: item.path))


def _resources(manifest: Mapping) -> tuple[tuple[str, str], ...]:
    """Every (role, alias) the manifest binds, in a fixed order.

    The invocations in the order the manifest records them, and the roles of
    one invocation sorted by name, so the same runs always produce the same
    public labels and so the archive always comes out the same bytes.
    """
    found: list[tuple[str, str]] = []
    for invocation in manifest.get("invocations") or ():
        if not isinstance(invocation, Mapping):
            continue
        if invocation.get("host"):
            found.append(("host", str(invocation["host"])))
        resources = invocation.get("resources") or {}
        if not isinstance(resources, Mapping):
            continue
        for role in sorted(str(name) for name in resources):
            found.append((role, str(resources[role])))
    return tuple(found)


def _timestamp(event: Mapping | None) -> str:
    return str((event or {}).get("timestamp") or "")


def _elapsed(first: Mapping | None, last: Mapping | None) -> float:
    if not first or not last:
        return 0.0
    try:
        start = datetime.datetime.fromisoformat(str(first["timestamp"]))
        end = datetime.datetime.fromisoformat(str(last["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return 0.0
    return (end - start).total_seconds()


def read_run(directory: Path | str) -> RunEvidence:
    """One finished run's contribution, from its ``run.jsonl``.

    Nothing is read from the bench and nothing is written: a run directory
    copied off the test host answers the same as the original.
    """
    directory = Path(directory)
    run_log = directory / RUN_LOG if directory.is_dir() else directory
    if not run_log.is_file():
        raise EvidenceError(f"{directory} holds no {RUN_LOG}")
    events = load_events(run_log)
    if not events:
        raise EvidenceError(f"{run_log} recorded nothing")
    manifest = _details(events, "manifest").get("manifest") or {}
    started = _details(events, "run_started")
    finished = _last(events, "run_finished")
    summary = dict((finished or {}).get("details") or {})
    plan = str(manifest.get("plan_name") or started.get("plan_name") or "")
    suite = str(manifest.get("suite") or started.get("suite") or "")
    failures, blocked, not_applicable = [], [], []
    skipped = 0
    for event in events:
        kind = event.get("event_type")
        details = event.get("details") or {}
        if kind == "test_finished" and event.get("status") not in (
            "passed", "skipped", "not_applicable"
        ):
            failures.append((
                str(details.get("invocation_id") or details.get("test_name") or ""),
                f"{event.get('status')}: {details.get('reason') or 'no reason recorded'}",
            ))
        elif kind == "test_finished":
            skipped += int(details.get("skipped_cases") or 0)
        elif kind == "blocked":
            blocked.append((str(details.get("test_name") or ""),
                            str(details.get("reason") or "")))
        elif kind == "not_applicable":
            not_applicable.append((
                str(details.get("invocation_id") or details.get("test_name") or ""),
                str(details.get("reason") or ""),
            ))
    watchdog = dict((_last(events, "watchdog_summary") or {}).get("details") or {})
    stage = dict((_last(events, "bootloader_stage_finished") or {}).get("details") or {})
    users, hosts = _holders(events)
    strip = {str(directory.resolve())}
    sources = tuple(manifest.get("sources") or started.get("sources") or ())
    for source in sources:
        if isinstance(source, Mapping) and source.get("source_path"):
            strip.add(str(source["source_path"]).rstrip("/"))
    return RunEvidence(
        directory=directory,
        run_id=str(events[0].get("run_id") or "unknown"),
        plan=plan,
        suite=suite,
        gate=gate_of(suite),
        host_alias=str(manifest.get("host") or started.get("host") or ""),
        platform=_platform(manifest),
        status=(finished or {}).get("status", "incomplete"),
        started=_timestamp(events[0]),
        ended=_timestamp(events[-1]),
        duration_s=_elapsed(events[0], events[-1]),
        sources=sources,
        resources=_resources(manifest),
        requested_tests=int(summary.get("requested_tests") or 0),
        completed_tests=int(summary.get("completed_tests") or 0),
        failures=tuple(failures),
        blocked=tuple(blocked),
        not_applicable=tuple(not_applicable),
        skipped_cases=skipped,
        watchdog_resets=int(summary.get("watchdog_resets")
                            or watchdog.get("resets") or 0),
        bootloader_mode=str(stage.get("mode") or ""),
        failure_reason=str(summary.get("failure_reason") or ""),
        measurements=_measurements(events),
        files=_files(run_log, events),
        private=Private(
            serials=tuple(sorted(run_serials(events))),
            users=tuple(sorted(users)),
            hosts=tuple(sorted(hosts)),
            paths=tuple(sorted(strip)),
        ),
    )


# ---------------------------------------------------------------------------
# Public labels for the bench's own aliases
# ---------------------------------------------------------------------------

# A role name's own ordinal, which the label does not keep: `guard Ubertooth
# 1` and `guard Ubertooth 2` are two aliases bound as the same kind of thing,
# and the label numbers the aliases rather than repeating the role's number.
_ROLE_ORDINAL = re.compile(r"\s+\d+$")


def _role_label(role: str) -> str:
    return _ROLE_ORDINAL.sub("", role.strip()) or "device"


def public_labels(runs: Sequence[RunEvidence]) -> dict[str, str]:
    """A public label for every registry alias the runs name.

    The bench's aliases are its own: a host registered under its host name,
    or a board under a name that says where it sits, would go out with the
    release. Each alias becomes the kind of thing it was bound as and a
    number -- ``Ubertooth 1``, ``guard Ubertooth 2``, ``host 1`` -- taken
    from the role the manifest records it under, which is the catalog's name
    for that role and so already public.

    The numbering is by first appearance over the runs in the order given,
    and the roles of one invocation are read in sorted order, so the same
    runs always produce the same labels and the archive always comes out the
    same bytes.
    """
    labels: dict[str, str] = {}
    counts: dict[str, int] = {}

    def label(alias: str, kind: str) -> None:
        alias = str(alias or "").strip()
        if not alias or alias in labels:
            return
        counts[kind] = counts.get(kind, 0) + 1
        labels[alias] = f"{kind} {counts[kind]}"

    for run in runs:
        label(run.host_alias, "host")
        for role, alias in run.resources:
            label(alias, _role_label(role))
    return labels


def private_names(runs: Sequence[RunEvidence],
                  strip_paths: Iterable[str] = ()) -> Private:
    """Everything one export has to take out, over all of its runs.

    Merged rather than per run: a board one run names appears in another
    run's transcript as well (the guard reads, the isolation enumeration),
    and a bundle whose second run still named it would publish it.
    """
    merged = Private()
    for run in runs:
        merged = merged.merged(run.private)
    merged = merged.with_paths(str(path).rstrip("/") for path in strip_paths)
    return Private(
        serials=merged.serials, users=merged.users, hosts=merged.hosts,
        aliases=public_labels(runs), paths=merged.paths,
    )


# ---------------------------------------------------------------------------
# Gate completeness
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """What one gate has behind it in this export."""

    gate: Gate
    runs: tuple[RunEvidence, ...] = ()
    attestation: str = ""

    @property
    def met(self) -> bool:
        """Is there evidence, and does it say the gate passed?

        A run that failed is evidence of a failure, not of a gate: the
        summary lists the gate as missing and the run under Deviations, so
        nobody reads a red run as a green gate.
        """
        if self.attestation:
            return True
        return bool(self.runs) and all(run.passed for run in self.runs)

    def describe(self) -> str:
        if self.attestation:
            return f"attested by hand: {self.attestation}"
        if not self.runs:
            return "no evidence"
        return ", ".join(
            f"`{run.suite}` on {run.platform} ({run.status})" for run in self.runs
        )


def gate_results(runs: Sequence[RunEvidence],
                 attestations: Mapping[str, str] | None = None,
                 ) -> tuple[GateResult, ...]:
    """Every gate with the runs and attestations behind it, in table order.

    The bootloader gate is the one a suite does not name: a run satisfies it
    by having compared or installed a bootloader (``--bootloader check`` or
    ``update``), which its own records say, and otherwise it waits for an
    attestation like the installer's.
    """
    by_gate: dict[str, list[RunEvidence]] = {}
    attestations = dict(attestations or {})
    for run in runs:
        by_gate.setdefault(run.gate, []).append(run)
    bootloader = [
        run for run in runs
        if run.bootloader_mode in BOOTLOADER_ACCEPTANCE_MODES
    ]
    found = []
    for gate in GATES:
        behind = list(by_gate.get(gate.key, ()))
        if gate.key == BOOTLOADER_GATE:
            behind = bootloader
        found.append(GateResult(
            gate, tuple(behind), str(attestations.get(gate.key, "") or "")))
    return tuple(found)


def missing_gates(results: Sequence[GateResult], phase: str) -> tuple[GateResult, ...]:
    """The gates this phase requires and this evidence does not meet."""
    return tuple(
        result for result in results
        if result.gate.required_for(phase) and not result.met
    )


def check_attestations(attestations: Mapping[str, str]) -> None:
    """Refuse an attestation for a gate that does not exist, or one a suite
    answers: an attestation is a person's word, and it may stand only where
    no run can."""
    for key in attestations:
        gate = GATE_BY_KEY.get(key)
        if gate is None:
            raise EvidenceError(
                f"no gate named {key!r}; the gates are: "
                + ", ".join(item.key for item in GATES)
            )
        if not gate.by_hand:
            raise EvidenceError(
                f"gate {key!r} is measured by a run of {' or '.join(gate.suites)}, "
                "so it cannot be attested by hand"
            )


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

def _cell(value) -> str:
    """One table cell: a record's text cannot end the row or add a column."""
    text = str(value).replace("|", "\\|")
    return " ".join(text.split()) or "–"


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.1f} min"
    return f"{seconds:.1f} s"


def _source_rows(runs: Sequence[RunEvidence]) -> list[tuple[str, str]]:
    """The commit each source was resolved to, once, with a note if the runs
    disagree: evidence for a release has to be evidence of one build."""
    rows = []
    for name in ("Ubertooth", "libbtbb"):
        found = {}
        for run in runs:
            source = run.source(name)
            if source.get("commit"):
                found.setdefault(str(source["commit"]), []).append(run)
        if not found:
            continue
        for commit, behind in sorted(found.items()):
            source = behind[0].source(name)
            ref = str(source.get("ref_name") or source.get("requested_ref") or "")
            version = str(source.get("build_version") or "")
            detail = f"`{commit}`"
            if ref:
                detail += f" ({ref})"
            if version:
                detail += f", a build of it reports `{version}`"
            if len(found) > 1:
                detail += (
                    f" — {len(behind)} of {len(runs)} runs; **the runs do not "
                    "agree on this commit**"
                )
            rows.append((f"{name} commit", detail))
    return rows


def harness_commit(repository: Path | str | None = None) -> str:
    """The commit of the harness checkout this export is being made from.

    Not read from a run: a run manifest records the Ubertooth and libbtbb
    commits and the hashes of the plan and the registry it used, and not the
    harness's own revision. So the summary says which checkout composed the
    bundle, which is the honest claim, and ``--harness-commit`` is there for
    an operator who knows better.
    """
    path = Path(repository) if repository else Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@dataclass
class Export:
    """One export's inputs, as the summary and the archive both read them."""

    runs: tuple[RunEvidence, ...]
    release: str
    phase: str
    attestations: Mapping[str, str] = field(default_factory=dict)
    harness: str = ""
    private_bundle: bool = False
    allow_incomplete: bool = False
    now: datetime.datetime | None = None
    private: Private = field(default_factory=Private)

    @property
    def results(self) -> tuple[GateResult, ...]:
        return gate_results(self.runs, self.attestations)

    @property
    def missing(self) -> tuple[GateResult, ...]:
        return missing_gates(self.results, self.phase)

    @property
    def logs_name(self) -> str:
        return PRIVATE_LOGS_NAME if self.private_bundle else LOGS_NAME

    @property
    def summary_name(self) -> str:
        return PRIVATE_SUMMARY_NAME if self.private_bundle else SUMMARY_NAME

    @property
    def sums_name(self) -> str:
        return PRIVATE_SUMS_NAME if self.private_bundle else SUMS_NAME


def summary(export: Export, members: Sequence["Member"] = ()) -> str:
    """``validation-summary.md``, composed from the runs' own records.

    It is sanitized and scanned like every other asset: a failure reason, a
    measurement, or a platform string comes out of a run record and may
    carry a path.
    """
    runs = export.runs
    stamp = (export.now or datetime.datetime.now(datetime.timezone.utc)).replace(
        microsecond=0, tzinfo=None).isoformat()
    lines = [f"# Ubertooth `{export.release}` validation evidence", ""]
    missing = export.missing
    if missing:
        lines += [
            "> **Incomplete.** This evidence does not meet every gate a "
            f"{PHASE_TITLES[export.phase]} requires. What is missing is "
            "listed under [Missing required evidence]"
            "(#missing-required-evidence); nothing has been left out "
            "silently.", "",
        ]
    if export.private_bundle:
        lines += [
            "> **Private bundle.** Device serial numbers are kept here. Do "
            "not attach this to a public release; the public bundle is the "
            f"one named `{SUMMARY_NAME}`.", "",
        ]
    lines += [
        "Hardware validation of the Ubertooth fork on a registered bench, "
        "exported by the test harness. The procedure is the fork's release "
        "test plan; what each test proves is in the harness's own "
        "`docs/tests.md`.", "",
        "Everything here is sanitized for publication: local paths, machine "
        "and operator names, the bench's own aliases for its hosts and "
        "boards, device serial numbers, and captured traffic are removed or "
        "replaced by a placeholder. The unredacted records stay on the test "
        "host. Public repository URLs, release names, commits, tool and "
        "firmware versions, plan and suite names, timestamps, durations, "
        "verdicts, and measurements are kept, because they are the "
        "evidence.", "",
        "## Identity", "", "| | |", "|---|---|",
        f"| Release | `{_cell(export.release)}` |",
        f"| Phase | {_cell(PHASE_TITLES[export.phase])} |",
    ]
    for label, value in _source_rows(runs):
        lines.append(f"| {label} | {value} |")
    lines += [
        f"| Harness checkout | {('`' + export.harness + '`') if export.harness else 'not recorded'} |",
        f"| Runs | {len(runs)} |",
        f"| Exported (UTC) | {stamp} |",
        "", "## Gates", "",
        "| Gate | Required for this phase | Evidence |", "|---|---|---|",
    ]
    for result in export.results:
        if result.gate.key == OTHER_GATE and not result.runs:
            continue
        lines.append(
            f"| {_cell(result.gate.title)} "
            f"| {'yes' if result.gate.required_for(export.phase) else 'no'} "
            f"| {_cell(result.describe())} |"
        )
    lines += ["", "## Missing required evidence", ""]
    if missing:
        lines += [
            f"- **{result.gate.title}**: {result.describe()}"
            for result in missing
        ]
    else:
        lines.append(
            "None: every gate this phase requires has a passing run or an "
            "attestation behind it."
        )
    lines += [
        "", "## Runs", "",
        "| Gate | Plan / suite | Platform | Started (UTC) | Ended (UTC) "
        "| Duration | Tests passed | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        lines.append(
            f"| {_cell(GATE_BY_KEY[run.gate].title)} "
            f"| `{_cell(run.plan)}` / `{_cell(run.suite)}` "
            f"| {_cell(run.platform)} | {_cell(run.started)} "
            f"| {_cell(run.ended)} | {_duration(run.duration_s)} "
            f"| {run.completed_tests}/{run.requested_tests} "
            f"| {_cell(run.status)} |"
        )
    totals = {
        "Tests requested": sum(run.requested_tests for run in runs),
        "Tests passed": sum(run.completed_tests for run in runs),
        "Tests failed": sum(len(run.failures) for run in runs),
        "Tests blocked": sum(len(run.blocked) for run in runs),
        "Left out as not applicable": sum(len(run.not_applicable) for run in runs),
        "Cases skipped": sum(run.skipped_cases for run in runs),
        "Boards that reset themselves": sum(run.watchdog_resets for run in runs),
    }
    lines += ["", "## Totals", "", "| | |", "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in totals.items()]
    lines += ["", "## Measurements", ""]
    readings = [
        (run, reading) for run in runs for reading in run.measurements
    ]
    if readings:
        lines += [
            "Values the runs read out of the tools' own output and recorded.",
            "", "| Suite | Test | Measurement | Value |", "|---|---|---|---|",
        ]
        lines += [
            f"| `{_cell(run.suite)}` | {_cell(reading.test)} "
            f"| {_cell(reading.name)} | {_cell(reading.value)} |"
            for run, reading in readings
        ]
    else:
        lines.append("None of these runs recorded an observed value.")
    lines += ["", "## Deviations", ""]
    deviations = []
    for run in runs:
        where = f"`{run.suite}` on {run.platform}"
        if not run.passed:
            deviations.append(
                f"- {where}: the run finished **{run.status}**"
                + (f" — {run.failure_reason}" if run.failure_reason else "")
            )
        for test, reason in run.failures:
            deviations.append(f"- {where}: test `{test}` {reason}")
        for test, reason in run.blocked:
            deviations.append(
                f"- {where}: test `{test}` was **blocked** — {reason}")
        for test, reason in run.not_applicable:
            deviations.append(
                f"- {where}: `{test}` was not applicable — {reason}")
        if run.watchdog_resets:
            deviations.append(
                f"- {where}: {run.watchdog_resets} board self-reset(s); the "
                "run's own Watchdog section has the record"
            )
    reruns = _reruns(runs)
    for suite, count in reruns:
        deviations.append(
            f"- `{suite}` was run {count} times in this evidence; both results "
            "are included, and a rerun is a deviation to account for"
        )
    lines += deviations or ["None recorded."]
    lines += ["", "## Included logs", ""]
    if members:
        lines += [
            f"`{export.logs_name}` carries these, each sanitized the same "
            f"way, with `{MANIFEST_NAME}` naming the SHA-256 of every one.",
            "", "| Member | Bytes |", "|---|---|",
        ]
        lines += [
            f"| `{_cell(member.name)}` | {member.size} |" for member in members
        ]
    else:
        lines.append("None.")
    held_back = [
        (run, file) for run in runs for file in run.files if not file.is_text
    ]
    lines += ["", "## Files kept on the test host", ""]
    if held_back:
        lines += [
            "These the runs saved and the bundle does not carry: a capture "
            "holds whatever traffic was on the air, which is not ours to "
            "publish, and a binary dump cannot be sanitized. Each is named "
            "with the size and SHA-256 the run recorded, so a reader can ask "
            "for one and check they were given it.",
            "", "| Suite | File | Bytes | SHA-256 |", "|---|---|---|---|",
        ]
        lines += [
            f"| `{_cell(run.suite)}` | `{_cell(file.path)}` | {file.bytes} "
            f"| `{_cell(file.sha256)}` |"
            for run, file in held_back
        ]
    else:
        lines.append("None: every file these runs saved is in the bundle.")
    lines += [
        "", "## Checking this bundle", "",
        "```sh",
        f"sha256sum -c {export.sums_name}",
        f"tar --zstd -xf {export.logs_name}",
        f"sha256sum -c {MANIFEST_NAME}",
        "```", "",
    ]
    return "\n".join(lines) + "\n"


def _reruns(runs: Sequence[RunEvidence]) -> tuple[tuple[str, int], ...]:
    """Suites this evidence holds more than one run of, and how many.

    A suite rerun without a code change is allowed once and has to be
    recorded (release-testing.md section 7), so two runs of one suite are
    reported rather than counted twice and forgotten. A suite run on two
    platforms is not a rerun: the platform is part of the result.
    """
    counts: dict[tuple[str, str], int] = {}
    for run in runs:
        key = (run.suite, run.platform)
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        (suite, count) for (suite, _platform), count in sorted(counts.items())
        if count > 1
    )


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    """One file in the archive, as it will be written."""

    name: str
    path: Path
    size: int
    sha256: str


def _sanitize_into(source: Path, destination: Path, private: Private,
                   where: str) -> Member:
    """Sanitize one file into the staging tree, scanning as it is written.

    A line at a time, so a soak's run.jsonl costs one line of memory rather
    than its own size, and so a finding names the line it was found on. The
    scan runs on what was written, and the first finding ends the export:
    a half-sanitized bundle is worse than none.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open(encoding="utf-8", errors="replace") as reading, \
            destination.open("w", encoding="utf-8", newline="\n") as writing:
        for number, line in enumerate(reading, start=1):
            ending = "\n" if line.endswith("\n") else ""
            clean = sanitize.sanitize(line.rstrip("\n"), private)
            found = sanitize.line_findings(
                clean, private, where=where, number=number)
            if found:
                raise sanitize.Unsafe(
                    f"{where} cannot be published:\n" + sanitize.describe(found)
                    + "\n\nSanitize the source, or name the prefix with "
                    "--strip-path; the unredacted records stay on the test host."
                )
            text = clean + ending
            writing.write(text)
            encoded = text.encode("utf-8")
            digest.update(encoded)
            size += len(encoded)
    return Member(where, destination, size, digest.hexdigest())


def stage_members(export: Export, staging: Path) -> tuple[Member, ...]:
    """Every member of the archive, sanitized into ``staging``.

    One directory per run, named by its run ID, holding the run's report,
    its event log, and each text output file it saved. The run ID is a UTC
    timestamp and a random suffix, so it names the run on the test host
    without naming the host.
    """
    members: list[Member] = []
    for run in export.runs:
        base = run.directory if run.directory.is_dir() else run.directory.parent
        wanted = [
            (base / REPORT, f"runs/{run.run_id}/{REPORT}"),
            (base / RUN_LOG, f"runs/{run.run_id}/{RUN_LOG}"),
        ]
        for file in run.files:
            if not file.is_text:
                continue
            try:
                # A recorded path is relative to the run directory and must
                # stay inside it; a path that is absolute, climbs out, or
                # passes through a symbolic link is refused rather than read,
                # as the GUI refuses one.
                source = confined(base, file.path)
            except (OutsideRun, OSError, ValueError):
                continue
            wanted.append((source, f"runs/{run.run_id}/{file.path}"))
        for source, name in wanted:
            if not source.is_file():
                continue
            members.append(_sanitize_into(
                source, staging / name, export.private, name))
    return tuple(sorted(members, key=lambda member: member.name))


def manifest_text(members: Sequence[Member]) -> str:
    """``MANIFEST-SHA256``, in the format ``sha256sum -c`` reads."""
    return "".join(f"{member.sha256}  {member.name}\n" for member in members)


def sums_text(files: Sequence[tuple[str, str]]) -> str:
    return "".join(f"{digest}  {name}\n" for digest, name in files)


def _entry(name: str, size: int) -> tarfile.TarInfo:
    """A member's header with nothing of this host in it.

    No modification time, no owner, no group, and a fixed mode: the archive
    has to come out the same bytes from the same runs, and a header that
    carried a timestamp or a user id would both break that and publish
    something.
    """
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def write_archive(path: Path, members: Sequence[Member]) -> str:
    """Write the compressed tar and return its SHA-256.

    Members are written in sorted order with ``MANIFEST-SHA256`` last, since
    the manifest covers the others and cannot cover itself. The result is
    deterministic for a given zstandard version and :data:`COMPRESSION_LEVEL`,
    both pinned by this repository, so rebuilding the bundle from the
    retained records reproduces the hash the release published.
    """
    try:
        import zstandard
    except ImportError as error:  # pragma: no cover - the pin installs it
        raise EvidenceError(
            "the zstandard package is not installed, so "
            f"{LOGS_NAME} cannot be written; run python3 bootstrap.py"
        ) from error
    digest = hashlib.sha256()
    manifest = manifest_text(members).encode("utf-8")
    compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as raw:
        hashing = _Hashing(raw, digest)
        with compressor.stream_writer(hashing, closefd=False) as stream:
            # A stream of its own ("w|"), so nothing seeks back over what was
            # written and the archive can be produced through a compressor.
            with tarfile.open(fileobj=stream, mode="w|",
                              format=tarfile.PAX_FORMAT) as archive:
                for member in members:
                    with member.path.open("rb") as handle:
                        archive.addfile(_entry(member.name, member.size), handle)
                archive.addfile(_entry(MANIFEST_NAME, len(manifest)),
                                io.BytesIO(manifest))
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, path)
    return digest.hexdigest()


class _Hashing:
    """A write-only file that hashes what goes through it.

    So the archive is hashed as it is written rather than read back
    afterwards: a soak bundle is large, and the hash is what the release
    publishes.
    """

    def __init__(self, stream, digest):
        self._stream = stream
        self._digest = digest

    def write(self, data) -> int:
        self._digest.update(data)
        return self._stream.write(data)

    def flush(self) -> None:
        self._stream.flush()


def upload_command(export: Export) -> str:
    """The exact ``gh release upload`` line, for a person to run.

    Never run here, and not offered at all for an incomplete or private
    bundle: publishing is the decision this module exists to prepare, and a
    command printed beside a list of missing gates would invite skipping the
    reading.
    """
    if export.private_bundle:
        return ""
    if export.missing and not export.allow_incomplete:
        return ""
    return (
        f"gh release upload {export.release} \\\n"
        f"  {SUMMARY_NAME} {LOGS_NAME} {SUMS_NAME} \\\n"
        f"  --repo {FORK_REPOSITORY}"
    )


@dataclass(frozen=True)
class Bundle:
    """What one export wrote, and what the operator still has to decide."""

    directory: Path
    summary: Path
    logs: Path
    sums: Path
    members: tuple[Member, ...]
    missing: tuple[GateResult, ...]
    upload: str

    @property
    def complete(self) -> bool:
        return not self.missing


def build_bundle(
    run_directories: Sequence[Path | str],
    output: Path | str,
    *,
    release: str,
    phase: str | None = None,
    attestations: Mapping[str, str] | None = None,
    strip_paths: Iterable[str] = (),
    harness: str | None = None,
    private_bundle: bool = False,
    allow_incomplete: bool = False,
    now: datetime.datetime | None = None,
) -> Bundle:
    """Build the three assets into ``output`` and write nothing else.

    No hardware, no network, and no run directory is modified: an export can
    be made from copies of the records, and made again.
    """
    if not run_directories:
        raise EvidenceError("name at least one finished run directory")
    if not release:
        raise EvidenceError("--release names the release or tag this evidence is for")
    attestations = dict(attestations or {})
    check_attestations(attestations)
    phase = phase or phase_of_release(release)
    if phase not in PHASES:
        raise EvidenceError(f"phase must be one of {', '.join(PHASES)}")
    runs = tuple(read_run(directory) for directory in run_directories)
    runs = tuple(sorted(runs, key=lambda run: (run.started, run.run_id)))
    export = Export(
        runs=runs, release=release, phase=phase, attestations=attestations,
        harness=harness if harness is not None else harness_commit(),
        private_bundle=private_bundle, allow_incomplete=allow_incomplete, now=now,
    )
    export.private = private_names(runs, strip_paths)
    if private_bundle:
        # A private bundle keeps the serials, which is the only thing it
        # keeps: the paths, the machine and operator names, and the aliases
        # go out of it too, because none of them is ever wanted. The scan
        # keeps every other rule, so a private bundle is still refused for
        # anything but a serial.
        export.private = Private(
            serials=(), users=export.private.users, hosts=export.private.hosts,
            aliases=export.private.aliases, paths=export.private.paths,
            keep_serials=True,
        )
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evidence-", dir=directory) as scratch:
        members = stage_members(export, Path(scratch))
        text = sanitize.require_safe(
            sanitize.sanitize(summary(export, members), export.private),
            export.private, where=export.summary_name,
        )
        summary_path = directory / export.summary_name
        logs_path = directory / export.logs_name
        sums_path = directory / export.sums_name
        _write(summary_path, text.encode("utf-8"))
        logs_digest = write_archive(logs_path, members)
    summary_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _write(sums_path, sums_text((
        (logs_digest, export.logs_name),
        (summary_digest, export.summary_name),
    )).encode("utf-8"))
    return Bundle(
        directory=directory, summary=summary_path, logs=logs_path,
        sums=sums_path, members=members, missing=export.missing,
        upload=upload_command(export),
    )


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
