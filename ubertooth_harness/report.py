"""Render readable Markdown reports from a run's ``run.jsonl``.

One run produces two files: ``run.jsonl`` (every record, written live) and
``report.md`` (compiled from it when the run ends). The report starts with the
setup and results, then the full transcript: every line timestamped, sections
marked with ``=== ... ===`` headers, and each command shown as the command sent
followed by its result and response (the shared raw-log format).
"""

import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping, Sequence

from ubertooth_harness import targets
from ubertooth_harness.events import REPORT, RUN_LOG
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.leases import (
    describe_holder,
    describe_pairing_lease,
    describe_queued_resource,
    describe_takeover,
)
from ubertooth_harness import outputfiles
from ubertooth_harness.outputfiles import CHECKS_EVENT, output_file_list
from ubertooth_harness.rawlog import (
    BANNER_WIDTH,
    NOTE,
    RECORD_OUTPUT_BYTES,
    RECORD_OUTPUT_EVENTS,
    RECORD_TAIL_BYTES,
    RECORD_TAIL_EVENTS,
    RESULT_TAG,
    TAGS,
    Chunk,
    CommandCapture,
    count_text,
    format_command_record,
    inline,
    line_shape,
    stage_label,
    verdict,
)
from ubertooth_harness.rf import describe as describe_placement
from ubertooth_harness.watchdog import wait_counts


REDACTED = "<serial>"
# Fallback cap for a command whose output run.jsonl holds in full, because the
# run was recorded before output was capped as it was written: this much of
# the start and of the end of it is rendered. Both limits are rawlog's, and
# both matter: the size bounds a few huge records, the count bounds many tiny
# ones, which cost a timestamp and a tag each in the transcript. rawlog counts
# the bytes a record takes in run.jsonl, JSON escapes included, and the report
# counts the characters it renders, so the same limit is a little more
# generous here.
LEGACY_OUTPUT_HEAD_CHARS = RECORD_OUTPUT_BYTES
LEGACY_OUTPUT_HEAD_RECORDS = RECORD_OUTPUT_EVENTS
LEGACY_OUTPUT_TAIL_CHARS = RECORD_TAIL_BYTES
LEGACY_OUTPUT_TAIL_RECORDS = RECORD_TAIL_EVENTS
LEGEND = (
    "Times are elapsed since the run started (UTC start in Time above). "
    "Tags: `[C]` command sent, `[O]` stdout, `[E]` stderr, `[X]` the execution "
    "result (exit code and duration, never a verdict), "
    "`[S]` state step, `[I]` power isolation, `[M]` harness record, "
    "`[<alias> >]` a line sent to that known target's serial shell and "
    "`[<alias> <]` a line it answered. "
    "`+++` opens a step and `---` closes it; `===` marks the run, build, "
    "flash, preflight, and each test."
)


def load_events(path: Path | str) -> list[dict]:
    """Read run.jsonl; a truncated final line (crash mid-write) is ignored."""
    events = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break
    return events


def _time(event: Mapping) -> datetime.datetime:
    """Naive UTC; older runs carried a +00:00 suffix."""
    moment = datetime.datetime.fromisoformat(event["timestamp"])
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return moment


# Fewest characters a serial needs to be masked wherever it appears rather
# than only where it stands alone (_redact). A SEGGER probe's serial is nine
# digits or more, so this is about the short width of a registry entry like
# `000123`, which the tools write as `123`: masking every "123" inside every
# longer number would mangle the transcript, and a standalone one is masked
# either way. The padded width of that same entry is 12 digits, so it is
# masked everywhere, including inside a device path. A bridge serial is 1 to
# 64 characters of the registry's choosing (resources.py), so a short one
# can read as a word rather than a number; the same width settles both, and
# what counts as standing alone differs (_redact).
_SERIAL_MIN_CHARS = 6
# How wide a DK's by-id name writes its J-Link serial (targets.DK_BY_ID).
_SERIAL_PADDED_DIGITS = 12


def _serial_widths(value: str) -> set[str]:
    """The other widths one numeric serial is written in.

    A J-Link serial reaches the run in two spellings of the same number: as
    the registry holds it, which is how `nrfutil device list` prints it
    (``001050210121`` on this bench), and as a number, because nrfdk.py takes
    it as one and preflight.py puts it on the command line with the leading
    zeros gone (``1050210121``). A DK's by-id name pads it to 12 digits
    again. ``--redact-serials`` has to mask each of them, or the serial
    survives in the transcript. Any numeric serial is treated this way, so a
    numeric bridge serial gains a padded width nothing writes, which costs
    nothing; one written in hex is masked as the registry holds it.
    """
    if not value.isdigit():
        return set()
    number = value.lstrip("0")
    # Past 12 digits it is not a J-Link serial but an Ubertooth's, which is
    # 32 hex digits and masked as it is written.
    if len(number) > _SERIAL_PADDED_DIGITS:
        return set()
    if not number:
        # All zeros, which the registry takes. The number the tools write is
        # "0", and masking that would mask every 0 in the transcript.
        return {value.rjust(_SERIAL_PADDED_DIGITS, "0")}
    return {number, number.rjust(_SERIAL_PADDED_DIGITS, "0")}


class _Run:
    """Facts about one run, gathered from its records."""

    def __init__(self, events: Sequence[Mapping]):
        self.events = list(events)
        self.run_id = self.events[0]["run_id"] if self.events else "unknown"
        self.started = next((e for e in self.events if e["event_type"] == "run_started"), {})
        manifest = next((e for e in self.events if e["event_type"] == "manifest"), {})
        self.manifest = manifest.get("details", {}).get("manifest", {})
        self.finished = next(
            (e for e in reversed(self.events) if e["event_type"] == "run_finished"), None
        )
        self.tests = []
        open_tests = {}
        for event in self.events:
            details = event.get("details", {})
            if event["event_type"] == "test_started":
                record = {"started": event, "finished": None}
                open_tests[details["invocation_id"]] = record
                self.tests.append(record)
            elif event["event_type"] == "test_finished":
                record = open_tests.get(details.get("invocation_id"))
                if record is not None:
                    record["finished"] = event
        # Left out before the run, because the host named cannot run them or
        # this bench has not got the hardware (hostneeds.py, planner.py):
        # neither skipped nor failed.
        self.not_applicable = [
            e.get("details", {}) for e in self.events
            if e["event_type"] == "not_applicable"
        ]
        # Required tests whose hardware is not registered (planner.py): on
        # this host, or on a host this registry has not got at all. Also
        # left out before the run, but the run fails rather than passing
        # with the gate unmeasured.
        self.blocked = [
            e.get("details", {}) for e in self.events
            if e["event_type"] == "blocked"
        ]
        self.capabilities = next(
            (e.get("details", {}).get("capabilities") for e in self.events
             if e["event_type"] == "host_capabilities"), None
        )
        self.preflight = [
            e for e in self.events
            if e["event_type"] == "check_finished"
            and e.get("details", {}).get("phase") == "preflight"
        ]
        # Which known device each test ran against, by invocation (#186).
        self.known_targets = {
            e["details"]["invocation_id"]: e["details"]
            for e in self.events
            if e["event_type"] == "known_target"
            and e.get("details", {}).get("invocation_id")
        }

    @property
    def status(self) -> str:
        if self.finished is None:
            return "incomplete (no run_finished record)"
        return self.finished["status"]

    def serials(self) -> set[str]:
        """Every device serial the run names, in every width it is written:
        resources (an Ubertooth's serial, a DK's J-Link serial, an ESP32
        peer's bridge serial), preflight, flash guards, and boards seen by
        the isolation enumeration checks.

        A device path that carries a serial is not one of them. ``_redact``
        masks the serial inside the path, which leaves the rest of it to read
        ("port /dev/serial/by-id/usb-SEGGER_J-Link_<serial>-if00" rather than
        "port <serial>") and works for a path the registry never held: off
        Linux the harness discovers the port at run time, and nrfdk.py prints
        it.
        """
        found = set()

        def add(value):
            if isinstance(value, str) and value:
                found.update((value, value.lower()))
                found.update(_serial_widths(value))

        identities = [
            identity
            for invocation in self.manifest.get("invocations", [])
            for identity in invocation.get("resource_identities", {}).values()
        ]
        for event in self.events:
            details = event.get("details", {})
            if event["event_type"] == "check_started":
                identities.append(details.get("resource_identity", {}))
            elif event["event_type"] == "build_flash_started":
                for board in details.get("boards", []):
                    add(board.get("serial"))
            elif event["event_type"] == "flash_run_started":
                add(details.get("serial"))
                for serial in (*details.get("serials", []), *details.get("guard_serials", [])):
                    add(serial)
            elif event["event_type"].startswith("isolation_"):
                for serial in details.get("enumerated", []):
                    add(serial)
        for identity in identities:
            for key, value in identity.items():
                # A device path is not a serial: it holds one, and _redact
                # masks it inside the path, wherever the path came from.
                if "serial" in key and not str(value).startswith("/"):
                    add(value)
        return found


def _duration(first: Mapping | None, last: Mapping | None) -> str:
    if not first or not last:
        return ""
    seconds = (_time(last) - _time(first)).total_seconds()
    return f"{seconds:.1f}s"


def _cell(value) -> str:
    # A line ending of any kind would end the table row, and an unescaped
    # pipe would add a column; values come from run records, which are data.
    text = str(value).replace("|", "\\|")
    for ending in ("\r\n", "\n", "\r"):
        text = text.replace(ending, " ")
    return text


# The registry identity key that names a device differs by kind: an Ubertooth
# has `serial`, an nRF52840 DK its J-Link probe's `jlink serial`. The keys are
# listed rather than matched on the word "serial", which would also pick up a
# DK's `serial device` and print a /dev/serial/by-id path.
# An Ubertooth is named by its own serial, a DK by its J-Link probe's, and
# an ESP32 Classic peer by its USB bridge's: a peer has no serial of its
# own, and the bridge serial is what the registry and the lab lease it by.
_IDENTITY_SERIAL_KEYS = ("serial", "jlink serial", "bridge serial")


def _identity_serial(identity: Mapping) -> str:
    """The serial that names this device, whichever kind of device it is."""
    for key in _IDENTITY_SERIAL_KEYS:
        value = identity.get(key)
        if value:
            return str(value)
    return ""


def _resource_text(details: Mapping) -> str:
    """role=alias for every bound resource, with its RF placement where the
    registry records one, so a result reads next to what it was taken at."""
    placements = details.get("rf")
    # A record written by another version of the harness may hold anything.
    if not isinstance(placements, Mapping):
        placements = {}
    return ", ".join(
        f"{role}={alias}"
        + (f" [{describe_placement(placements[role])}]" if role in placements else "")
        for role, alias in details.get("resources", {}).items()
    )


def _placement_rows(run: _Run) -> list[tuple[str, str]]:
    """One row naming the RF placement of every board the run used.

    What each role asked of its placement is per invocation, so it stays in
    the run record (`placement_requirements`, `rf_required`) rather than
    being restated here: a plan with several steps of one test can ask
    different things of the same role.
    """
    placed = {}
    for invocation in run.manifest.get("invocations") or []:
        resources = invocation.get("resources") or {}
        for role, record in (invocation.get("resource_placements") or {}).items():
            alias = resources.get(role)
            if alias:
                placed[alias] = record
    if not placed:
        return []
    return [(
        "RF placement",
        "; ".join(
            f"{alias}: {describe_placement(record)}"
            for alias, record in sorted(placed.items())
        ),
    )]


def _libbtbb_in_the_tools(run: _Run) -> str:
    """Which libbtbb preflight required of the run's host tools, and whether
    it found it (#294).

    Empty only for a record written before this check existed. A run that
    required nothing says so: that is the condition #294 is about, and a
    reader must be able to tell it from a run that verified its library.
    """
    wanted = not_checked = check_id = ""
    for event in run.events:
        details = event.get("details") or {}
        if event["event_type"] != "check_started":
            continue
        if details.get("expected_libbtbb") or details.get("libbtbb_not_checked"):
            wanted = str(details.get("expected_libbtbb", ""))
            not_checked = str(details.get("libbtbb_not_checked", ""))
            check_id = str(details.get("check_id", ""))
            break
    if not_checked:
        return f"**not checked**: {not_checked}"
    if not wanted:
        return ""
    status = next(
        (
            check["status"] for check in run.preflight
            if (check.get("details") or {}).get("check_id") == check_id
        ),
        "",
    )
    if status == "passed":
        return f"{wanted} (verified)"
    # The check can fail on the Ubertooth revision and stop before it reads
    # the libbtbb half, and a check that never ran has no status at all.
    # Either way the requirement was not reached, and saying it failed would
    # blame the library for something it was never compared against.
    if status and _compared_libbtbb(run, check_id):
        return f"**{status}**: {wanted}"
    return f"**not reached**: {wanted}"


def _compared_libbtbb(run: _Run, check_id: str) -> bool:
    """Did the host version check get as far as reading the libbtbb half?

    The check records every pattern it reads, so its own record is what says
    whether this comparison happened (outputfiles.py).
    """
    for event in run.events:
        details = event.get("details") or {}
        if event["event_type"] != CHECKS_EVENT or details.get("check_id") != check_id:
            continue
        for check in details.get("checks") or []:
            if str((check or {}).get("name", "")).endswith(" libbtbb"):
                return True
    return False


def _devices(run: _Run) -> dict[str, dict]:
    """Every device preflight read, by registry alias: the serial that names
    it and the firmware the run established for it.

    Preflight binds each selected and guard device, so this is what the run
    was measured on. The firmware is the revision the check required and how
    it went; a flash test writes the firmware it verifies, so its own record
    replaces that with what the board was left on.
    """
    devices: dict[str, dict] = {}
    started = {
        e["details"].get("check_id"): e["details"]
        for e in run.events if e["event_type"] == "check_started"
        and e["details"].get("phase") == "preflight"
    }
    for check in run.preflight:
        details = check.get("details", {})
        start = started.get(details.get("check_id"), {})
        alias = start.get("resource")
        if not alias:
            continue
        identity = start.get("resource_identity", {})
        device = devices.setdefault(alias, {
            "serial": _identity_serial(identity),
            "firmware": "",
        })
        if details.get("check_name") == "Firmware version":
            state = "verified" if check["status"] == "passed" else check["status"]
            device["firmware"] = f"{start.get('expected_revision', '')} ({state})"
    for event in run.events:
        if event["event_type"] == "flash_run_finished":
            details = event["details"]
            finals = {
                str(serial).lower(): revision
                for serial, revision in (details.get("final_revisions") or {}).items()
            }
            if "final_revisions" not in details and details.get("serial"):
                finals = {str(details["serial"]).lower(): details.get("final_revision")}
            attempted = details.get("attempted_flashes_by_serial")
            if attempted is None:  # records from before per-board counts
                attempted = {str(details.get("serial", "")): details.get("attempted_flashes", 0)}
            attempted = {str(serial).lower(): count for serial, count in attempted.items()}
            for device in devices.values():
                serial = device["serial"].lower()
                if attempted.get(serial):
                    device["firmware"] = (
                        f"written by this run; final {finals.get(serial) or 'unknown'}"
                    )
                elif serial in finals:
                    device["firmware"] = (
                        f"not written by this run; read {finals[serial] or 'unknown'}"
                    )
    return devices


def _setup_rows(run: _Run) -> list[tuple[str, str]]:
    manifest = run.manifest
    rows = []
    started = run.started.get("details", {})
    plan = manifest.get("plan_name") or started.get("plan_name", "")
    suite = manifest.get("suite") or started.get("suite", "")
    rows.append(("Run", f"`{run.run_id}`"))
    rows.append(("Plan / suite", f"{plan} / {suite}"))
    summary = run.finished["details"] if run.finished else {}
    if summary:
        result = (
            f"**{run.status}**: {summary.get('completed_tests', 0)}/"
            f"{summary.get('requested_tests', 0)} tests, "
            f"{summary.get('completed_preflight_checks', 0)}/"
            f"{summary.get('requested_preflight_checks', 0)} preflight checks, "
            f"{summary.get('skipped_cases', 0)} skipped cases"
        )
        if summary.get("not_applicable_cases") or summary.get("not_applicable_tests"):
            result += (
                f", {summary.get('not_applicable_cases', 0)} cases and "
                f"{summary.get('not_applicable_tests', 0)} tests not applicable"
            )
        if summary.get("blocked_tests"):
            result += (
                f", {summary['blocked_tests']} tests blocked (see Blocked)"
            )
        if summary.get("watchdog_resets"):
            result += (
                f", {summary['watchdog_resets']} board self-resets (see Watchdog)"
            )
        if summary.get("failure_reason"):
            result += f"; {summary['failure_reason']}"
    else:
        result = f"**{run.status}**"
    rows.append(("Result", result))
    if run.events:
        rows.append((
            "Time (UTC)",
            f"started {run.events[0]['timestamp']}, last record "
            f"{run.events[-1]['timestamp']} ({_duration(run.events[0], run.events[-1])})",
        ))
    environment = manifest.get("environment", {})
    os_info = environment.get("os", {})
    host = manifest.get("host") or started.get("host", "")
    platform = " ".join(
        str(item) for item in (
            os_info.get("system"),
            os_info.get("macos_version") or os_info.get("distribution"),
            os_info.get("release"),
            os_info.get("architecture"),
        ) if item
    )
    rows.append(("Host", f"{host} ({platform})" if platform else host))
    # A run that failed before its manifest still names its sources.
    for source in manifest.get("sources") or started.get("sources", []):
        # "`main` at `<commit>`" when the commit was chosen by a name (a
        # controller run); otherwise the ref as given resolved to the commit.
        ref_name = source.get("ref_name")
        if ref_name and ref_name != source.get("requested_ref"):
            ref = f"`{ref_name}` at `{source.get('commit')}`"
        else:
            ref = f"`{source.get('requested_ref')}` = `{source.get('commit')}`"
        rows.append((
            source.get("name", "source"),
            f"{ref} ({source.get('expected_revision')}) from {source.get('repository')}",
        ))
    pin = next((e for e in run.events if e["event_type"] == "libbtbb_pin"), None)
    if pin is not None:
        # How the libbtbb built compares with the fork's install.sh pin (#85).
        status = pin.get("status", "")
        message = pin.get("details", {}).get("message", "")
        rows.append((
            "libbtbb pin",
            message if status == "passed" else f"**{status}**: {message}",
        ))
    loaded = _libbtbb_in_the_tools(run)
    if loaded:
        rows.append(("libbtbb in the tools", loaded))
    parts = next((e for e in run.events if e["event_type"] == "dfu_part_table"), None)
    if parts is not None:
        # Whether the flash sizes a DFU read-back is checked against are the
        # ones the fork's part table has at the commit under test (#283).
        status = parts.get("status", "")
        message = parts.get("details", {}).get("message", "")
        rows.append((
            "DFU part table",
            message if status == "passed" else f"**{status}**: {message}",
        ))
    build = manifest.get("build") or {}
    if build:
        toolchain = ", ".join(f"{k} {v}" for k, v in sorted(build.get("toolchain", {}).items()))
        rows.append(("Built", f"yes, into the run prefix; {toolchain}"))
        rows.append(("Built tools report", build.get("host_version", "")))
    else:
        rows.append(("Built", "no (installed tools)"))
    rows.append(("Flash", _flash_row(run, bool(build))))
    capabilities = run.capabilities
    if capabilities:
        tools = capabilities.get("bluez_tools")
        rows.append((
            "Host can run",
            f"{capabilities.get('platform')} ({capabilities.get('basis')}); BlueZ tools: "
            + ("not known" if tools is None else ", ".join(tools) or "none")
            + "; Python tools build: " + _known(capabilities.get("python_tools"))
            + "; firmware build: " + _known(capabilities.get("firmware_build")),
        ))
    python = environment.get("python", {})
    if python:
        deps = ", ".join(f"{k} {v}" for k, v in sorted(environment.get("dependencies", {}).items()))
        rows.append(("Python", f"{python.get('version')} (pip {environment.get('pip')}; {deps})"))
    devices = _devices(run)
    if devices:
        rows.append((
            "Devices",
            "; ".join(
                f"{alias} serial {d['serial']}" + (f": firmware {d['firmware']}" if d["firmware"] else "")
                for alias, d in devices.items()
            ),
        ))
    artifacts = manifest.get("artifacts", [])
    held = manifest.get("leases") or []
    host_lock = [item for item in held if item.get("key") == "host-power"]
    external = [item for item in held if item.get("external")]
    held = [
        item for item in held
        if item.get("key") != "host-power" and not item.get("external")
    ]
    if host_lock:
        rows.append((
            "Host lock",
            f"since {host_lock[0].get('acquired_at')} (held for the whole run: "
            "no other harness power change or firmware write on this host)",
        ))
    if held:
        rows.append((
            "Board leases",
            "; ".join(
                f"{item.get('resource')} since {item.get('acquired_at')}" for item in held
            ) + " (held for the whole run)",
        ))
    if external:
        rows.append((
            "External board locks",
            "; ".join(f"{item.get('resource')} {item.get('lock')}" for item in external)
            + " (held for the whole run)",
        ))
    rows.extend(_takeover_rows(manifest))
    rows.extend(_queued_rows(manifest))
    rows.extend(_pairing_rows(manifest))
    rows.extend(_placement_rows(run))
    if artifacts:
        rows.append((
            "Artifacts",
            "; ".join(f"{a['name']} sha256 {a['sha256'][:12]}" for a in artifacts),
        ))
    return rows


def _takeover_rows(manifest: Mapping) -> list[tuple[str, str]]:
    """Leases of commands that were gone and this run took over to start.

    A takeover does not promise the device was idle: something still held
    that lock file, and it may have been a leftover command on the device.
    So a reader of an odd result later in the run needs to see it here, not
    only on the standard error of whoever started the run.
    """
    taken_over = manifest.get("lease_takeovers") or []
    if not taken_over:
        return []
    return [(
        "Leases taken over",
        "; ".join(
            f"{item.get('resource')} at {item.get('taken_at')} "
            f"from {describe_holder(item.get('holder') or {})}: {item.get('reason')} "
            f"({item.get('lock')})"
            for item in taken_over
        ) + " (a takeover does not promise the device was idle; a leftover "
        "command of that session may have been using it)",
    )]


def _queued_rows(manifest: Mapping) -> list[tuple[str, str]]:
    """Devices that were free while this run waited for its turn (#234).

    A run that wants several devices gives way to an older command doing
    the same for as long as anything that command asked for is held, so a
    lease nobody will give back (one held by a stuck command, or recorded
    on another host) can keep this run queued for its whole wait with the
    bench looking idle. This row says which devices those were, which
    command had the turn, and what was in its way, so a late run explains
    itself and a reader knows what to clear.
    """
    queued = manifest.get("lease_queue_waits") or []
    if not queued:
        return []
    return [(
        "Queued behind another command",
        # The wait is over by the time a report is read, so the line reads
        # as what happened, not as what is happening.
        "; ".join(describe_queued_resource(item, past=True) for item in queued),
    )]


def _pairing_rows(manifest: Mapping) -> list[tuple[str, str]]:
    """Devices this run held because they are wired to ones it named (#289).

    The leases above list them like any other, so without this row a reader
    sees a debug programmer leased by a run that asked for a board. The
    by-hand commands say it on standard error; a run says it here.
    """
    pairings = manifest.get("lease_pairings") or []
    if not pairings:
        return []
    return [(
        "Leases from a wired pair",
        "; ".join(describe_pairing_lease(item) for item in pairings),
    )]


def _flash_row(run: _Run, built: bool) -> str:
    """Whether the run flashed its boards with its own build first (#135)."""
    finished = next(
        (e for e in run.events if e["event_type"] == "build_flash_finished"), None
    )
    if finished is None:
        started = any(e["event_type"] == "build_flash_started" for e in run.events)
        if started:
            return "**did not finish**"
        return "no (the run does not build)" if not built else "no"
    details = finished.get("details", {})
    status = finished.get("status", "")
    if status == "skipped":
        return f"no ({details.get('reason', '')})"
    boards = "; ".join(
        f"{item.get('resource')} "
        + _flash_board_text(item)
        for item in details.get("boards", [])
    )
    text = f"with the build's firmware {details.get('expected_revision', '')}: {boards}"
    started = next(
        (e for e in run.events if e["event_type"] == "build_flash_started"), {}
    )
    if started.get("details", {}).get("host_lock"):
        written = any(item.get("written") for item in details.get("boards", []))
        text += (
            " (host lock held while writing)" if written else " (host lock held)"
        )
    if status != "passed":
        return f"**{status}**: {details.get('reason', '')}; {text}"
    return f"yes, verified before preflight; {text}"


def _flash_board_text(item: Mapping) -> str:
    """What happened to one board in the flash stage."""
    if item.get("written"):
        return (
            f"written, was {item.get('before') or 'unreadable'}, "
            f"now {item.get('after') or 'unknown'}"
        )
    if item.get("after"):
        return f"already ran {item['after']}"
    # The stage stopped before this board was read or written.
    return "not written"


def _known(value) -> str:
    return "not known" if value is None else "yes" if value else "no"


def _blocked_section(run: _Run) -> list[str]:
    """Required tests that never ran: the hardware they are about is not
    registered, so the run cannot pass without them. Either this host has
    not got it, or the step named a host this registry has not got at all."""
    if not run.blocked:
        return []
    lines = [
        "", "## Blocked", "",
        "Required tests whose hardware is not registered: on this host, or "
        "on a host this registry has not got. They never ran, so the run "
        "fails: unlike a not-applicable test, each one is about the hardware "
        "that is missing.", "",
        "| Test | Host | Reason |", "|---|---|---|",
    ]
    for item in run.blocked:
        lines.append(
            f"| {_cell(item.get('test_name', ''))} "
            f"| {_cell(item.get('host', ''))} | {_cell(item.get('reason', ''))} |"
        )
    return lines


def _not_applicable_section(run: _Run) -> list[str]:
    """What the run left out, and why: the host named cannot run it, or this
    bench has not got the hardware it binds."""
    if not run.not_applicable:
        return []
    lines = [
        "", "## Not applicable", "",
        "Left out before the run, because the host named cannot run them or "
        "this bench has not got the hardware; not skipped and not failed.", "",
        "| Test | Case | Host | Reason |", "|---|---|---|---|",
    ]
    for item in run.not_applicable:
        test = item.get("invocation_id") or item.get("test_name", "")
        lines.append(
            f"| {_cell(test)} | {_cell(item.get('case_name') or '(whole test)')} "
            f"| {_cell(item.get('host', ''))} | {_cell(item.get('reason', ''))} |"
        )
    return lines


def _bootloader_section(run: _Run) -> list[str]:
    """Which bootloader each board has, and what the run did about it.

    A board keeps its bootloader through every firmware update, so this is a
    property of the board rather than of the run: it is reported for every
    run, and a run that installed one says so in the same row.
    """
    records = [
        event.get("details", {}) for event in run.events
        if event["event_type"] == "bootloader_identity"
    ]
    if not records:
        return []
    # One row per board. In `update` mode there are two records for a board:
    # the stage's, which says it wrote one, and preflight's later read, which
    # says what is there now. The row takes the later identity and keeps the
    # earlier record's `written` and `previous`, because "this run installed
    # it" is the fact this section exists to carry.
    rows: dict[str, dict] = {}
    for details in records:
        name = str(details.get("resource") or details.get("serial") or "")
        earlier = rows.get(name)
        if earlier is not None and earlier.get("written"):
            details = {
                **details,
                "written": True,
                "previous": earlier.get("previous"),
                "status": (
                    "updated" if details.get("status") == "current"
                    else details.get("status")
                ),
                "reason": details.get("reason") or earlier.get("reason", ""),
            }
        rows[name] = details
    stage = next(
        (event.get("details", {}) for event in reversed(run.events)
         if event["event_type"] == "bootloader_stage_finished"), {}
    )
    mode = stage.get("mode") or next(
        (details.get("mode") for details in records if details.get("mode")), ""
    )
    expected = next(
        (details.get("expected") for details in records if details.get("expected")),
        stage.get("expected"),
    )
    lines = [
        "", "## Bootloader", "",
        "The first 16 KiB of a board's flash, which an ordinary firmware "
        "write cannot replace: a board keeps the bootloader it came with "
        "until somebody installs another one over a programmer on its "
        "in-system programming header. `ubertooth-util -B` is what reports "
        "it.", "",
        f"Mode: `{mode or 'record'}`. "
        + (
            "Expected: " + _cell(_bootloader_text(expected))
            if expected else
            "Nothing was compared: this run recorded what each board has."
        ),
        "",
        "| Board | Serial | Bootloader | Verdict | Installed by this run | Note |",
        "|---|---|---|---|---|---|",
    ]
    for name, details in rows.items():
        installed = details.get("installed")
        lines.append(
            f"| {_cell(name)} | {_cell(details.get('serial', ''))} "
            f"| {_cell(_bootloader_text(installed))} "
            f"| {_cell(details.get('status', ''))} "
            f"| {'yes' if details.get('written') else 'no'} "
            f"| {_cell(details.get('reason') or '–')} |"
        )
    return lines


def _bootloader_text(identity) -> str:
    """One identity as a reader reads it, from its record."""
    if not isinstance(identity, Mapping):
        return "not reported"
    crc = identity.get("region_crc32") or ""
    if identity.get("has_version_block"):
        return f"{identity.get('version', '')} {identity.get('build', '')}, CRC32:{crc}"
    return f"{identity.get('detail') or identity.get('generation', '')}, CRC32:{crc}"


def _watchdog_section(run: _Run) -> list[str]:
    """What the firmware's watchdog record said, per board, and the fork
    issue to file for a board that reset itself.

    A self-reset is the watchdog working, so it is reported here rather than
    failing the run; `ubertooth-util -w` is what the run read (watchdog.py).
    """
    summary = next(
        (e.get("details", {}) for e in reversed(run.events)
         if e["event_type"] == "watchdog_summary"), None
    )
    if summary is None:
        return []
    lines = [
        "", "## Watchdog", "",
        "The firmware resets a board that stops servicing USB or whose mode "
        "stops looping, and keeps the reason in RAM a reset does not clear. "
        "A reset is reported, not failed: the board came back by itself. "
        "Each row holds what that board's last whole record said, and what "
        "its last read found.", "",
        "| Board | Serial | Reset itself | Last boot cause | Last read | "
        "Transfer errors | Radio waits that gave up |", "|---|---|---|---|---|---|---|",
    ]
    for board in summary.get("boards", []):
        waits = board.get("radio_waits")
        counts = wait_counts(waits) if waits else "not reported"
        errors = board.get("transfer_errors")
        lines.append(
            f"| {_cell(board.get('resource', ''))} | {_cell(board.get('serial', ''))} "
            f"| {board.get('resets', 0)} | {_cell(board.get('boot_cause') or '–')} "
            f"| {_cell(board.get('state', ''))} "
            f"| {'not reported' if errors is None else errors} | {_cell(counts)} |"
        )
    warnings = [
        line
        for event in run.events if event["event_type"] == "watchdog"
        for line in event.get("details", {}).get("warnings", [])
    ]
    notes = [
        line
        for event in run.events if event["event_type"] == "watchdog"
        for line in event.get("details", {}).get("notes", [])
    ]
    for heading, items in (("Warnings", warnings), ("Notes", notes)):
        if items:
            lines += ["", f"**{heading}**", ""]
            lines += [f"- {item}" for item in items]
    for offer in summary.get("issues", []):
        issue = offer.get("issue", {})
        duplicates = offer.get("duplicates", {})
        lines += [
            "", f"### Issue to file on {issue.get('repository', 'the fork')}", "",
            f"Board {_cell(offer.get('resource', ''))} reset itself "
            f"{offer.get('resets', 0)} time(s). File this, or comment on an open "
            "issue below if it is the same one.", "",
        ]
        matches = duplicates.get("matches") or []
        if matches:
            lines += ["Open fork issues matching this mode and wait:", ""]
            lines += [
                f"- [#{match.get('number')}]({match.get('url')}) {match.get('title')}"
                for match in matches
            ]
            lines.append("")
        elif duplicates.get("error"):
            lines += [
                f"No duplicate search: {duplicates['error']} "
                f"(search: `{duplicates.get('terms', '')}`).", "",
            ]
        else:
            lines += [
                f"No open fork issue matches `{duplicates.get('terms', '')}`.", "",
            ]
        # Four backticks: the body carries the `-w` output in a fence of its own.
        lines += ["````text", f"Title: {issue.get('title', '')}", ""]
        lines += str(issue.get("body", "")).splitlines()
        lines.append("````")
    return lines


# Sizes in the Output files table: the magnitude a reader wants at a glance,
# and the exact count beside it.
_UNITS = (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10))
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _size(count: int) -> str:
    for name, step in _UNITS:
        if count >= step:
            return f"{count / step:.1f} {name} ({count} bytes)"
    return f"{count} bytes"


def _check_failed(check) -> bool:
    """Did this recorded check come out against the run?

    Not every kind records a reason when it fails: a step that failed on an
    observed setting, or on a missing ``SKIP:`` line, says so in its own
    step record and leaves the check's reason empty. So what the check
    found decides as well.
    """
    if check.reason:
        return True
    if check.found is None:
        return False
    if check.kind == "forbidden":
        return check.found
    return not check.found


def _checks_cell(saved) -> str:
    """The checks that read one file, and how they came out."""
    if not saved.checks:
        return "none"
    failed = [check for check in saved.checks if _check_failed(check)]
    kinds = {}
    for check in saved.checks:
        kinds[check.kind] = kinds.get(check.kind, 0) + 1
    counts = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    if not failed:
        return f"{counts}: all passed"
    named = "; ".join(
        f"{check.name}: {check.reason or _not_found(check)}" for check in failed[:3]
    )
    if len(failed) > 3:
        named += f"; and {len(failed) - 3} more"
    return f"{counts}: {len(failed)} against the run ({named})"


def _not_found(check) -> str:
    if check.kind == "forbidden":
        return f"{check.pattern!r} was found"
    return f"{check.pattern!r} was not found"


def _hash_cell(value: str) -> str:
    """A SHA-256 as a code span, or the value escaped when it is not one.

    A run record is data, so a value that is not a digest must not be able to
    add a column to the table.
    """
    return f"`{value}`" if _SHA256.fullmatch(value) else _cell(value)


def _output_files_section(run: _Run) -> list[str]:
    """The files the run saved, by test, with the checks that read each one.

    Together with the SHA-256 this is what lets a reader check a result
    afterwards from the report alone, and `ubertooth-harness validate` run
    the same checks again.
    """
    saved = outputfiles.saved_output(run.events)
    if not saved:
        return []
    names = {
        record["started"]["details"].get("invocation_id"):
            record["started"]["details"].get("test_name", "")
        for record in run.tests
    }
    lines = ["", "## Output files", ""]
    groups: dict[str, list] = {}
    for item in saved:
        groups.setdefault(item.group, []).append(item)
    for group, items in groups.items():
        named = names.get(group)
        lines += [
            f"### {group}" + (f": {named}" if named else ""), "",
            "| File | Stream | Size | Lines | SHA-256 | Checks |",
            "|---|---|---|---|---|---|",
        ]
        for item in items:
            lines.append(
                f"| {_cell(item.file.path)} | {_cell(item.file.stream)} "
                f"| {_size(item.file.bytes)} "
                f"| {item.file.lines or ''} | {_hash_cell(item.file.sha256)} "
                f"| {_cell(_checks_cell(item))} |"
            )
        lines.append("")
    return lines


def _known_target_section(run: _Run) -> list[str]:
    """One line per test naming the known device it ran against, or none."""
    if not run.tests or not run.known_targets:
        return []
    lines = ["", "## Known targets", "",
             "Which device the harness drove for each test, so a capture "
             "against a known device is told apart from one of whatever "
             "traffic was there. The serial, port, and address are what the "
             "registry records and the test's own ready case checks; the "
             "firmware version is the one that device's driver requires, and "
             "the board's own answer is in the transcript.", ""]
    for record in run.tests:
        invocation = record["started"]["details"].get("invocation_id", "")
        details = run.known_targets.get(invocation)
        if details is None:
            # A test the host could not run drove nothing, and a run recorded
            # before the harness kept this record says nothing either way.
            continue
        lines.append(f"- `{invocation}`: {details.get('summary', '')}")
    return lines


def _target_cell(run: _Run, invocation: str) -> str:
    details = run.known_targets.get(invocation)
    if details is None:
        return ""
    return targets.column(details.get("targets") or ())


def _results_table(run: _Run) -> list[str]:
    lines = [
        "| Test | Host and resources | Known target | Result | Failed cases "
        "| Skipped cases | Not applicable cases | Duration | Reason |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for record in run.tests:
        started = record["started"]
        details = started["details"]
        finished = record["finished"]
        resources = _resource_text(details)
        where = details.get("host", "") + (f" ({resources})" if resources else "")
        if finished is None:
            status, failed, skipped, reason = "did not finish", "", "", ""
            not_applicable = ""
        else:
            fd = finished["details"]
            status = finished["status"]
            failed = fd.get("failed_cases", "")
            # Guard and watchdog checks are not cases; show them beside the
            # case count, so a test that failed on one of them does not read
            # as a failure with nothing failed.
            for count, what in (
                # "state", as the live stream says it: one vocabulary for
                # the one recorded number, `failed_guard_checks` (#342).
                (fd.get("failed_guard_checks", 0), "state"),
                (fd.get("failed_watchdog_checks", 0), "watchdog"),
            ):
                if count:
                    failed = f"{failed} + {count} {what} check{'s' if count != 1 else ''}"
            skipped = fd.get("skipped_cases", "")
            not_applicable = fd.get("not_applicable_cases", "")
            reason = fd.get("reason", "")
            # A board that reset itself does not fail the test, so the table
            # says it happened and the Watchdog section below has the lines
            # themselves; printing them twice in one report helps nobody.
            count = fd.get("watchdog_warnings", 0)
            if count:
                note = (
                    f"warning: {count} watchdog warning"
                    f"{'' if count == 1 else 's'} (see Watchdog)"
                )
                reason = f"{reason}; {note}" if reason else note
        target = _target_cell(run, details.get("invocation_id", ""))
        lines.append(
            f"| {_cell(details.get('invocation_id'))} | {_cell(where)} "
            f"| {_cell(target)} | {_cell(status)} "
            f"| {failed} | {skipped} | {not_applicable} | {_duration(started, finished)} "
            f"| {_cell(reason)} |"
        )
    if not run.tests:
        lines.append("| (no tests started) | | | | | | | | |")
    return lines


def elapsed(origin: datetime.datetime):
    """Formatter for time since ``origin`` as HH:MM:SS.mmm."""
    def stamp(moment: datetime.datetime) -> str:
        if moment.tzinfo is not None:
            moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        total_ms = max(0, round((moment - origin).total_seconds() * 1000))
        seconds, ms = divmod(total_ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
    return stamp


# A line break other than the newline the output was split on. A line's
# shape can hold one: a tool printing its progress in place writes a
# carriage return, and the whole of that goes into one line of output.
# The transcript splits what it renders the way str.splitlines() does, so
# such a shape would arrive as two lines of the note, with the count and the
# tag on the first alone, and redaction reads a shape by that head
# (_SHAPE_NOTE_LINE): the rest of the shape would go out as it is. Written
# the way a reader can see it instead, each shape on its own line.
_BREAK_IN_A_SHAPE = re.compile("[\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
_BREAK_NAMES = {"\r": r"\r", "\v": r"\v", "\f": r"\f"}


def _one_line(text: str) -> str:
    """``text`` with every line break but a newline written visibly."""
    def written(match: re.Match) -> str:
        char = match.group(0)
        if char in _BREAK_NAMES:
            return _BREAK_NAMES[char]
        return f"\\u{ord(char):04x}" if ord(char) > 0xFF else f"\\x{ord(char):02x}"

    return _BREAK_IN_A_SHAPE.sub(written, text)


def _omitted_text(note: Mapping) -> str:
    """[M] note text for output kept in the output/ files, not run.jsonl."""
    summary = note.get("output_omitted") or {}
    lines = summary.get("lines") or {}
    chars = summary.get("chars") or {}
    counts = ", ".join(
        f"{lines.get(stream, 0)} {stream} lines ({chars.get(stream, 0)} characters)"
        for stream in ("stdout", "stderr")
        if lines.get(stream) or chars.get(stream)
    )
    text = (
        f"full output in {output_file_list(note)}; left out of run.jsonl here: "
        f"{counts or 'nothing'}\n"
    )
    for shape in summary.get("common_shapes") or []:
        text += (
            f"  {shape.get('count', 0)} x {TAGS.get(shape.get('stream'), '[?]')} "
            f"{_one_line(str(shape.get('shape', '')))}\n"
        )
    rest = summary.get("distinct_shapes", 0) - len(summary.get("common_shapes") or [])
    if rest > 0:
        text += f"  and {rest} other line shapes\n"
    return text


def _output_time(output: Mapping) -> datetime.datetime:
    """When a chunk arrived; tail records carry it in ``received``."""
    received = output["details"].get("received")
    return _time({"timestamp": received}) if received else _time(output)


def _recorded_with_a_cap(outputs: Sequence[Mapping], finished: Mapping) -> bool:
    """Whether run.jsonl capped this command's output as it was recorded.

    A command whose output passed the record limits leaves a
    ``command_output_omitted`` record and names its output file in
    ``command_finished``. Without either, the records hold everything the
    command printed, which is how a run recorded before the cap was written.
    """
    if any(o["event_type"] == "command_output_omitted" for o in outputs):
        return True
    # ``output_file`` is one combined file; ``output_files`` is the key the
    # per-stream files take (#194), so a run recorded by either is read.
    return bool(finished.get("output_file") or finished.get("output_files"))


def _take_chunks(
    chunks: Sequence[Chunk], *, chars: int, records: int, from_end: bool
) -> tuple[list[Chunk], list[Chunk]]:
    """Chunks from one end of ``chunks``, within both limits.

    Returns what was taken and what the record the size limit fell inside
    leaves behind, which is the output left out next to that end. Taken from
    the start, the cut moves back to a line boundary where that record has
    one, so the gap that follows begins on a fresh line; a single line longer
    than the limit is cut inside instead of dropped whole.
    """
    taken: list[Chunk] = []
    left_out: list[Chunk] = []
    room = chars
    for moment, stream, text in chunks:
        if len(taken) >= records:
            break
        if len(text) <= room:
            taken.append((moment, stream, text))
            room -= len(text)
            continue
        cut = len(text) - room if from_end else room
        if not from_end:
            boundary = text.rfind("\n", 0, cut)
            if boundary >= 0:
                cut = boundary + 1
        kept, rest = (
            (text[cut:], text[:cut]) if from_end else (text[:cut], text[cut:])
        )
        if kept:
            taken.append((moment, stream, kept))
            if rest:
                left_out.append((moment, stream, rest))
        # With nothing kept from it, the record is wholly left out, and the
        # caller already has it among the records neither end reached.
        break
    return taken, left_out


def _tail_on_whole_lines(left_out: list[Chunk], tail: list[Chunk]) -> list[Chunk]:
    """Begin each stream's part of the tail at a line boundary.

    A record boundary is not a line boundary, so the first record of the tail
    can carry the end of a line whose start was left out, which would render
    as a whole line of its own. run.jsonl's tail starts at a line boundary
    for the same reason (``rawlog.CappedOutput.finish``).

    What this drops is appended to ``left_out``, so the caller counts it and
    stamps the gap where it really ends.
    """
    ends = {
        stream: text[-1]
        for _moment, stream, text in left_out if text
    }
    kept = []
    for moment, stream, text in tail:
        # A line left out can run through several of the tail's records, so
        # this holds until a newline actually arrives.
        if ends.get(stream, "\n") != "\n":
            rest_of_it, newline, text = text.partition("\n")
            if rest_of_it or newline:
                left_out.append((moment, stream, rest_of_it + newline))
            if not newline:
                ends[stream] = rest_of_it[-1:] or ends[stream]
                continue
            ends[stream] = "\n"
            if not text:
                continue
        kept.append((moment, stream, text))
    return kept


def _capped_chunks(chunks: Sequence[Chunk]) -> list[Chunk]:
    """The start and the end of a command's output, with the gap counted.

    The fallback cap of #125: run.jsonl caps each command's output as it is
    recorded, but a run recorded before that holds all of it, and rendering
    report.md for one printed megabytes. There is no output file to point at,
    so the note points at run.jsonl.
    """
    total = sum(len(text) for _moment, _stream, text in chunks)
    if (total <= LEGACY_OUTPUT_HEAD_CHARS + LEGACY_OUTPUT_TAIL_CHARS
            and len(chunks) <= LEGACY_OUTPUT_HEAD_RECORDS + LEGACY_OUTPUT_TAIL_RECORDS):
        return list(chunks)
    head, after_head = _take_chunks(
        chunks, chars=LEGACY_OUTPUT_HEAD_CHARS,
        records=LEGACY_OUTPUT_HEAD_RECORDS, from_end=False,
    )
    tail, before_tail = _take_chunks(
        list(reversed(chunks)), chars=LEGACY_OUTPUT_TAIL_CHARS,
        records=LEGACY_OUTPUT_TAIL_RECORDS, from_end=True,
    )
    tail.reverse()
    # Where the gap is, in arrival order: the rest of the record the head
    # stopped inside, the records neither end reached, and the start of the
    # record the tail begins inside. Only the last of these per stream is
    # read (the character before the tail, and the gap's time); where one
    # record holds both cuts, its middle is listed twice, so the size of
    # this is not the size of the gap, which is total less what is kept.
    left_out = (
        after_head
        + list(chunks[len(head):len(chunks) - len(tail)])
        + before_tail
    )
    # The tail can come back empty, when all of it belonged to a line whose
    # start was left out. run.jsonl's tail does the same, and the harness's
    # own last word (a timeout, a cancellation, an output sink error) is not
    # lost by it: the executor begins each of those on a line of its own.
    tail = _tail_on_whole_lines(left_out, tail)
    kept = sum(len(text) for _moment, _stream, text in head + tail)
    if kept >= total:  # one empty record over the record limit
        return list(chunks)
    # Stamped where the output left out ends, as a recorded note is (its
    # ``received``, from ``rawlog.CappedOutput._dropped_at``).
    moment = left_out[-1][0] if left_out else chunks[-1][0]
    note = (moment, NOTE, f"{total - kept} more characters of output are in run.jsonl\n")
    return head + [note] + tail


def _output_chunks(outputs: Sequence[Mapping], *, capped: bool) -> list[Chunk]:
    """One command's output records as capture chunks, in arrival order."""
    chunks = [
        (_output_time(o), NOTE, _omitted_text(o["details"]))
        if o["event_type"] == "command_output_omitted"
        else (_output_time(o), o["details"].get("stream", "stdout"),
              o["details"].get("text", ""))
        for o in outputs
    ]
    return chunks if capped else _capped_chunks(chunks)


def _command_record(
    started: Mapping, outputs: list[Mapping], finished: Mapping | None, stamp
) -> str:
    sd = started["details"]
    fd = finished["details"] if finished else {}
    # A command's output records are capped in run.jsonl as they are written
    # (rawlog.RECORD_*, the rest in output/), so the report shows every record
    # it kept; one recorded before that cap is capped here instead.
    chunks = _output_chunks(outputs, capped=_recorded_with_a_cap(outputs, fd))
    stdout = "".join(text for _moment, stream, text in chunks if stream == "stdout")
    stderr = "".join(text for _moment, stream, text in chunks if stream == "stderr")
    result = CommandResult(
        tuple(sd.get("argv", [])),
        fd.get("returncode", -1),
        stdout,
        stderr,
        float(fd.get("duration_s", 0.0)),
        bool(fd.get("timed_out", False)),
        bool(fd.get("cancelled", False)),
    )
    capture = CommandCapture(lambda: _time(started))
    capture.chunks = chunks
    capture.finished = _time(finished) if finished else None
    stopped = bool(finished) and finished.get("status") == "stopped"
    record = format_command_record(result, capture, stopped=stopped, stamp=stamp)
    if finished is None:
        tool = Path(result.argv[0]).name if result.argv else "command"
        record += (
            f"{stamp(_time(outputs[-1] if outputs else started))} "
            f"{RESULT_TAG} {tool} did not finish\n"
        )
    return record


def _transcript(run: _Run) -> list[str]:
    out: list[str] = []
    pending: dict[str, dict] = {}
    rotating = False  # set by flash_run_started when flashes alternate boards
    build_step = None
    stamp = elapsed(_time(run.events[0])) if run.events else (lambda moment: "")

    def line(event, text):
        # Section markers line up with the text after a "[X] " tag.
        indent = "" if text.startswith("[") else "    "
        out.append(f"{stamp(_time(event))} {indent}{text}")

    def close_build_step(event):
        nonlocal build_step
        if build_step is not None:
            line(event, f"--- {build_step['name']} EXIT: {build_step['status']} ---")
            build_step = None

    for event in run.events:
        kind = event["event_type"]
        details = event.get("details", {})
        status = event.get("status", "")
        if kind == "command_started":
            pending[details.get("command_id", "")] = {"started": event, "outputs": []}
            continue
        if kind in ("command_output", "command_output_omitted"):
            record = pending.get(details.get("command_id", ""))
            if record is not None:
                record["outputs"].append(event)
            continue
        if kind == "command_finished":
            record = pending.pop(details.get("command_id", ""), None)
            if record is not None:
                out.append(_command_record(record["started"], record["outputs"], event, stamp).rstrip("\n"))
            # A run recorded before the build said how each step ended
            # (build_step_finished) has only its commands to go on.
            if details.get("phase") == "build" and build_step is not None and status != "passed":
                build_step["status"] = status
            continue
        if kind == "run_started":
            run_title = (
                f"{details.get('plan_name', '')} / {details.get('suite', '')} "
                f"on {details.get('host', '')}"
            )
            line(event, f"=== RUN {event['run_id']}: {run_title} ===")
        elif kind == "manifest":
            line(event, "[M] manifest recorded")
        elif kind == "not_applicable":
            what = details.get("test_name", "")
            if details.get("case_name"):
                what += f" / {details['case_name']}"
            line(event, f"[M] not applicable on {details.get('host', '')}: {what}: "
                        f"{details.get('reason', '')}")
        elif kind == "blocked":
            line(event, f"[M] blocked on {details.get('host', '')}: "
                        f"{details.get('test_name', '')}: "
                        f"{details.get('reason', '')}")
        elif kind == "build_started":
            line(event, "=== BUILD ===")
        elif kind == "build_step_started":
            close_build_step(event)
            open_step = details.get("step_name") or details.get("step_id", "")
            line(event, f"+++ {open_step} +++")
            build_step = {
                "name": open_step, "status": "passed",
                "id": details.get("step_id"),
            }
            continue
        elif kind == "build_step_finished":
            # The step's own outcome, which its commands do not give: a step
            # may accept a command that exited non-zero. A record for some
            # other step leaves the open one to be closed as it was before.
            if build_step is not None and build_step["id"] == details.get("step_id"):
                build_step = None
            name = details.get("step_name") or details.get("step_id", "")
            line(event, f"--- {name} EXIT: {status}{_reason(details)} ---")
            continue
        elif kind == "build_finished":
            close_build_step(event)
            line(event, f"=== BUILD EXIT: {status}{_reason(details)} ===")
        elif kind == "build_flash_started":
            line(event, f"=== FLASH: {len(details.get('boards', []))} board(s) with the "
                        f"build's firmware {details.get('expected_revision', '')} ===")
        elif kind == "build_flash_board_started":
            line(event, f"+++ flash {details.get('resource')} ({details.get('serial')}) +++")
        elif kind == "build_flash_board_finished":
            line(event, f"--- flash {details.get('resource')} EXIT: {status}, read back "
                        f"{details.get('after') or 'nothing'}{_reason(details)} ---")
        elif kind == "build_flash_finished":
            if status == "skipped":
                line(event, f"[M] flash before preflight skipped{_reason(details)}")
            else:
                line(event, f"=== FLASH EXIT: {status}{_reason(details)} ===")
        elif kind == "bootloader_build_started":
            pin = details.get("pin", {})
            line(event, "=== BOOTLOADER BUILD: pinned to "
                        f"{pin.get('short_commit', '')} ===")
        elif kind == "bootloader_build_finished":
            artifact = details.get("artifact", {})
            built = _bootloader_text(artifact.get("identity")) if artifact else ""
            line(event, f"=== BOOTLOADER BUILD EXIT: {status}"
                        + (f", {built}" if built else "")
                        + f"{_reason(details)} ===")
        elif kind == "bootloader_stage_started":
            line(event, f"=== BOOTLOADER {str(details.get('mode', '')).upper()}: "
                        f"{len(details.get('boards', []))} board(s) ===")
        elif kind == "bootloader_identity":
            line(event, f"[M] {details.get('resource')} bootloader "
                        f"{_bootloader_text(details.get('installed'))} "
                        f"({details.get('status', '')})"
                        + (f": {details['reason']}" if details.get("reason") else ""))
        elif kind == "bootloader_update_started":
            line(event, f"+++ install bootloader on {details.get('resource')} "
                        f"through {details.get('programmer')} +++")
        elif kind == "bootloader_update_finished":
            line(event, f"--- install bootloader on {details.get('resource')} EXIT: "
                        f"{status}, now "
                        f"{_bootloader_text(details.get('installed'))} ---")
        elif kind == "bootloader_stage_finished":
            if status == "skipped":
                line(event, f"[M] bootloader stage skipped{_reason(details)}")
            else:
                line(event, f"=== BOOTLOADER EXIT: {status}{_reason(details)} ===")
        elif kind == "preflight_started":
            line(event, "=== PREFLIGHT ===")
        elif kind == "preflight_finished":
            line(event, f"=== PREFLIGHT EXIT: {status}{_reason(details)} ===")
        elif kind == "test_started":
            resources = ", ".join(f"{k}={v}" for k, v in details.get("resources", {}).items())
            suffix = f" ({resources})" if resources else ""
            line(event, f"=== TEST {details.get('invocation_id')}: {details.get('test_name')} on {details.get('host')}{suffix} ===")
        elif kind == "known_target":
            line(event, f"[M] {details.get('summary', '')}")
        elif kind == "lease_takeover":
            line(event, f"[M] {describe_takeover(details)}")
        elif kind == "lease_queued":
            line(event, f"[M] {describe_queued_resource(details, past=True)}")
        elif kind == "lease_pairing":
            line(event, f"[M] {describe_pairing_lease(details)}")
        elif kind == "test_finished":
            line(
                event,
                f"=== TEST {details.get('invocation_id')}: {details.get('test_name')} "
                f"EXIT: {status}{_reason(details)} ===",
            )
        elif kind == "check_started":
            name = details.get("check_name") or details.get("case_name") or details.get("check_id")
            line(event, f"+++ {name} +++")
        elif kind == "check_finished":
            name = details.get("check_name") or details.get("case_name") or details.get("check_id")
            tries = (f" in {details['attempts']} attempts"
                     if details.get("attempts") else "")
            line(event, f"--- {name} EXIT: {status}{tries}{_reason(details)} ---")
        elif kind == "watchdog":
            line(event, f"[M] watchdog {status} ({details.get('stage', '')}): "
                        f"{details.get('resource', '')} "
                        f"{details.get('new_resets', 0)} new self-reset(s), "
                        f"{details.get('resets', '?')} since power-on"
                        + _reason(details))
            for warning in details.get("warnings", []):
                line(event, f"[M] warning: {warning}")
            for note in details.get("notes", []):
                line(event, f"[M] note: {note}")
        elif kind == "watchdog_summary":
            counts = ", ".join(
                f"{board.get('resource')} {board.get('resets', 0)}"
                for board in details.get("boards", [])
            )
            line(event, f"[M] watchdog {status}: self-resets by board: {counts}")
        elif kind == "step_finished":
            # The same phase label and verdict the live stream says, so a
            # reader moving between the two reads one vocabulary (#342).
            # Each piece on one line: one record is one timestamped line
            # here, and a recorded value may carry a line ending (#342).
            name = inline(details.get("step_name") or "step")
            text = (
                f"[S] {stage_label(details.get('stage', 'step'))} "
                f"{verdict(status)}: {name}"
            )
            if details.get("attempt"):
                text += f" (attempt {details['attempt']})"
            if details.get("expected") is not None:
                text += (f" (expected {inline(details['expected'])}, "
                         f"observed {inline(details.get('observed'))})")
            elif details.get("observed") is not None:
                text += f" (recorded {inline(details['observed'])})"
            if details.get("reason"):
                # A step's reason opens with its own name, which this line
                # has said already (#342).
                reason = inline(details["reason"]).removeprefix(f"{name}: ")
                text += f": {reason}"
            line(event, text)
        elif kind == "flash_run_started":
            boards = details.get("serials") or [details.get("serial")]
            where = (
                f"alternating across {', '.join(map(str, boards))}" if len(boards) > 1
                else f"on {details.get('serial')}"
            )
            line(event, f"+++ flash reliability: {details.get('requested_flashes')} flashes {where} +++")
            rotating = len(boards) > 1
        elif kind == "flash_started":
            target = f" to {details.get('serial')}" if rotating else ""
            line(event, f"+++ flash {details.get('iteration')}: {details.get('image_label')}{target} (expected {details.get('expected_revision')}) +++")
        elif kind == "flash_finished":
            line(
                event,
                f"--- flash {details.get('iteration')} EXIT: {status}, read back "
                f"{details.get('actual_revision') or 'nothing'}{_reason(details)} ---",
            )
        elif kind == "flash_run_finished":
            failure = details.get("failure_reason")
            per_board = details.get("verified_flashes_by_serial") or {}
            order = details.get("serials") or list(per_board)
            split = (
                " (" + ", ".join(f"{serial}: {per_board.get(serial, 0)}" for serial in order) + ")"
                if len(per_board) > 1 else ""
            )
            line(
                event,
                f"--- flash reliability EXIT: {status}, {details.get('verified_flashes')}/"
                f"{details.get('requested_flashes')} verified{split}"
                + (f": {failure}" if failure else "") + " ---",
            )
        elif kind == "run_finished":
            line(event, f"=== RUN {status}{_reason(details, 'failure_reason')} ===")
        elif kind.startswith("isolation_"):
            text = f"[I] {kind.removeprefix('isolation_')} {status}"
            if kind == "isolation_started" and details.get("read_only"):
                text += ": read-only enumeration check (no power actions)"
            elif kind == "isolation_confirmed" and details.get("read_only"):
                text += ": selected board is the only Ubertooth enumerated"
            elif kind == "isolation_finished":
                text += _reason(details)
            line(event, text)
    for record in pending.values():
        out.append(_command_record(record["started"], record["outputs"], None, stamp).rstrip("\n"))
    if build_step is not None and run.events:
        line(run.events[-1], f"--- {build_step['name']} EXIT: did not finish ---")
    return out


def _reason(details: Mapping, key: str = "reason") -> str:
    reason = details.get(key) or ""
    return f": {reason}" if reason else ""


# Any standalone 32-hex-digit token is masked, since Ubertooth serials have
# that form (for example an unregistered board named in a failure reason);
# this may also mask an unrelated 32-hex value. Longer hex runs such as git
# SHAs (40) and sha256 digests (64) are left alone.
_SERIAL_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
# Where a command's output was cut inside a serial, what is left of it is
# neither the whole serial nor 32 digits wide, so neither rule above masks
# it (#214). The two output caps cut where the limit falls rather than where
# the text allows: run.jsonl caps each command as it is recorded, in pieces of
# a fixed size (rawlog.RECORD_PIECE_CHARS), and the report's fallback cap for
# a run recorded before that caps a long line by character count. Other
# records are cut to a character count too (a line shape, an enumeration, a
# DFU message), and this rule covers them as well. Either side of a cut can
# survive, at the end of the output kept before the gap or at the start of
# what follows it, so position says nothing about which runs of hex to read.
#
# A run of hex digits this wide is masked where a serial the run names holds
# it: 8 digits of a 32-digit serial still say which board it was, and an
# unrelated value that wide sitting inside a known serial is improbable. Two
# pieces are left to read, deliberately: one narrower than this, which a cut
# in the middle of a 10-digit J-Link serial can leave on both sides, because
# masking every 4-digit hex value would mangle the transcript; and a piece
# with no boundary of its own, left glued to hex digits that came before or
# after it in the output, because the whole run is then one longer value no
# serial holds. A serial in this output is written after a space, a colon, a
# slash, or an equals sign.
#
# A serial is tested in lower case, which also masks a piece written in the
# other case. A numeric serial is held in the registry's padded width too, so
# a device registered as `000123` makes a run of eight zeros a piece of a
# serial; no J-Link serial is that short.
_PARTIAL_HEX_DIGITS = 8
# A cut serial of a board the run never names (the case _SERIAL_TOKEN is there
# for while the serial is whole) can only be recognized by its shape: this
# many digits is as narrow as a run can be and still be a serial rather than
# something a tool printed, the view the token rule above takes of a
# standalone 32-hex value. A narrower piece of an unnamed board's serial is
# left to read.
_UNKNOWN_HEX_DIGITS = 24
# Up to 31 digits: a run of 32 is a serial the token rule masked, and a whole
# git SHA (40) or digest (64) is left alone. A SHA or a digest that a cut left
# this wide is masked as a serial, which is the price of a rule that has only
# the shape of a run to go on: the report says <serial> where a reader could
# have had a piece of a commit.
_UNKNOWN_HEX_LIMIT = 31
_HEX_RUN = re.compile(
    rf"(?<![0-9A-Fa-f])[0-9A-Fa-f]{{{_PARTIAL_HEX_DIGITS},}}(?![0-9A-Fa-f])"
)


def _looks_like_a_serial(piece: str) -> bool:
    """Whether a hex run could be part of a device serial.

    A serial that wide carries both letters and digits (a piece of 24 hex
    digits with no letter in it turns up about once in 100,000), while the
    long runs a tool prints for its own reasons are usually one or the other:
    a decimal counter, a bit string, a field of zeros or of ff.
    """
    return any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece)


def _mask_cut_serials(text: str, serials: Sequence[str]) -> str:
    """Mask what a cut inside a serial left behind."""
    # A serial this narrow leaves no piece that wide: it is masked whole.
    known = [
        serial.lower() for serial in serials
        if len(serial) > _PARTIAL_HEX_DIGITS
    ]

    def mask(match: re.Match) -> str:
        piece = match.group(0).lower()
        if any(piece in serial for serial in known):
            return REDACTED
        if (_UNKNOWN_HEX_DIGITS <= len(piece) <= _UNKNOWN_HEX_LIMIT
                and _looks_like_a_serial(piece)):
            return REDACTED
        return match.group(0)

    return _HEX_RUN.sub(mask, text)


# A line the record limits left out of run.jsonl is counted by its shape
# (rawlog.line_shape), and the shapes are listed in the `[M]` note as
# `3 x [E] <shape>`. A shape is not the line, so masking a serial does not
# mask it (#220): line_shape replaces every run of digits with N, so a line
# that named a board arrives with the serial's digits gone and its hex
# letters and their positions intact, which is enough to pick one board out
# of a small bench.
#
# The rule reads the shape where the report publishes it and nowhere else:
# the `[M]` note's own lines, one per shape (_omitted_text, from
# rawlog.OmittedOutput). Everything else on the page is a command's own
# output, a board's answer, or the report's prose, and a rule that reads
# like this one does not belong there: the shape of
# `a1b2c3d4e5f6face7890abcdef123456` holds `faceN`, and `bInterfaceNumber`
# is a line a tool really prints, so the rule would cut up output a reader
# needs. Inside the note that cost is paid and the README states it: the
# shape of such a line reads `bInter<serial>umber N of N`. Working on the
# note's lines also means a change to the note's format shows up as a
# failure of the test that renders a capped command through it, rather than
# as redaction quietly stopping. What it costs is that the rules read a
# rendered line rather than the record's own field: a record from another
# version of the harness whose count is not a whole number, or whose shape
# holds a line ending, renders a line this does not recognize, and that
# shape goes out as it is.
#
# In a shape, a whole known shape is masked wherever it sits, at every
# occurrence, overlapping ones as one mask: a serial that repeats itself
# shapes into something that begins again inside itself. It is masked
# however few hex letters it carries, because the run naming that serial
# settles the question by itself, the view _mask_cut_serials takes of a
# piece of a known serial. It needs two characters of its own, though, since
# the shape of a serial of 31 digits and a letter is `Na` and masking that
# would take the letter out of every word of the note.
#
# What a cut left of a shape is masked as a piece. The 200-character shape
# limit cuts a shape, and an output cap can cut the line a shape was counted
# from, so either end of one can arrive on its own. A piece needs an N of
# its own and four characters the serial held, because those characters are
# what names the board and a piece is all the rule has to go on: fewer
# (`NabN`, the shape of `1ab2`) sits in the shape of too much ordinary
# output to mask, and a piece with no N in it is a run of the serial's own
# characters, left to _mask_cut_serials and its width rule for the same
# reason: `cafe` and `dead` are words a tool prints.
_SHAPE_MIN_OWN_CHARS = 2
_PIECE_MIN_OWN_CHARS = 4
# One line of the `[M]` note that lists the shapes of the lines the record
# limits left out: `00:00:09.000 [M]   3 x [E] <shape>`. Anchored on the
# elapsed time the transcript stamps every line with, so a command's own
# output cannot read as a note of the harness's. The head is taken as it is
# and the shape is what the rules read.
_SHAPE_NOTE_LINE = re.compile(
    r"(?m)^(?P<head>\d{2,}:\d\d:\d\d\.\d\d\d \[M\][^\S\n]+\d+ x \[[^\]\n]*\] )"
    r"(?P<shape>.*)$"
)
# Case folded for comparison: a tool prints a serial in the case it likes,
# so the letters of a shape are matched in either. The fold maps the ASCII
# letters a serial can hold and nothing else, which keeps the folded text
# the same length as the text, so a position in one is a position in the
# other. (Lower-casing the whole string is not length preserving: `İ`
# grows.) The `N` line_shape leaves where a run of digits was is folded with
# the rest, so a shape whose own letters read as one of those N's is masked
# too: pinning it apart left a serial holding that letter unmasked whenever
# a tool printed the serial in another case, and masking a shape that reads
# like a known one costs a reader one line of a note.
_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                      "abcdefghijklmnopqrstuvwxyz")


def _fold(text: str) -> str:
    return text.translate(_FOLD)


# The runs line_shape replaced, counted in the serial rather than in its
# shape, because a serial may hold an N of its own.
_DIGIT_RUN = re.compile(r"\d+")


def _own_chars_of_piece(folded: str) -> int:
    """Characters of a piece of a shape that a serial held, as far as the
    piece can say: what it says about which board.

    Takes the piece folded, so an `n` is read as one line_shape left where a
    run of digits was whichever case the line was printed in. The piece
    alone cannot say whether the serial held that letter instead; for a
    serial that did, this counts low, which leaves a narrow piece to read
    rather than masking ordinary text.
    """
    return len(folded) - folded.count("n")


def _shape_pieces(shapes: Iterable[str]) -> tuple[set[str], int]:
    """Every piece of every known shape, and the width of the longest.

    Held as a set, because the rule asks of thousands of positions in a long
    report whether the characters there are a piece of some shape.
    """
    pieces = set()
    for shape in shapes:
        for start in range(len(shape)):
            for end in range(start + 1, len(shape) + 1):
                pieces.add(shape[start:end])
    return pieces, max((len(shape) for shape in shapes), default=0)


def _mask_one_shape(shape: str, known: Sequence[str], pieces: set[str],
                    longest: int) -> str:
    """One published shape, with every known shape and piece of one in it
    masked.

    The whole shapes go first, longest first: a piece taken at an earlier
    character could otherwise swallow the start of a shape that repeats
    itself and leave a tail too narrow to mask.
    """
    for one in known:
        shape = _mask_everywhere(shape, one)
    return _mask_shape_pieces(shape, pieces, longest)


def _mask_shape_pieces(shape: str, pieces: set[str], longest: int) -> str:
    """``shape`` with every piece of a known serial's shape masked.

    Read from the left, taking the longest piece the known shapes hold at
    each character: what a shape holds is a shape, so masking it is right,
    and a piece that starts later is shorter. Where no piece wide enough
    starts at this character, the character is kept and the next one tried.
    """
    folded = _fold(shape)
    out = []
    index = 0
    while index < len(shape):
        end = index
        # Every prefix of a piece of a shape is a piece of that shape, so
        # the longest one is reached by growing until there is no longer a
        # piece there.
        while (end < min(len(shape), index + longest)
               and folded[index:end + 1] in pieces):
            end += 1
        # Folded, like the lookup above: a shape printed in lower case
        # carries its placeholder as `n`, and a piece of one is still a
        # piece of a shape.
        piece = folded[index:end]
        if "n" in piece and _own_chars_of_piece(piece) >= _PIECE_MIN_OWN_CHARS:
            out.append(REDACTED)
            index = end
        else:
            out.append(shape[index])
            index += 1
    return "".join(out)


def _mask_everywhere(text: str, wanted: str) -> str:
    """``text`` with every occurrence of ``wanted`` masked, in either case.

    Overlapping occurrences included, as one mask: a serial that repeats
    itself (`abc-1abc-1abc-1`) shapes into something that can begin again
    inside itself, and a search that resumed after each match would leave
    the tail of the second occurrence to read.
    """
    folded, needle = _fold(text), _fold(wanted)
    found = []
    at = folded.find(needle)
    while at >= 0:
        start, stop = at, at + len(needle)
        if found and start <= found[-1][1]:
            found[-1] = (found[-1][0], max(found[-1][1], stop))
        else:
            found.append((start, stop))
        at = folded.find(needle, at + 1)
    if not found:
        return text
    out, end = [], 0
    for start, stop in found:
        out.append(text[end:start])
        out.append(REDACTED)
        end = stop
    out.append(text[end:])
    return "".join(out)


def _own_chars_of_serial(serial: str, shape: str) -> int:
    """Characters of a shape that are the serial's own, not an N standing
    where a run of its digits was: what the shape says about the board.

    Counted from the runs of digits the serial held, not from the N's in the
    shape, because a serial may hold an N of its own: a bridge serial is any
    string of letters, digits and ``_.:+-``.
    """
    return len(shape) - len(_DIGIT_RUN.findall(serial))


def _mask_serial_shapes(text: str, serials: Sequence[str]) -> str:
    """Mask the shape of a line that named a board the run names."""
    shapes = set()
    for serial in serials:
        shape = line_shape(serial)
        # No digits in it, so the shape is the serial itself, already masked.
        if shape == serial:
            continue
        # A numeric serial shapes to a bare `N`, and one of 31 digits and a
        # letter to `Na`: too little to mask, and `N` names nothing at all.
        if _own_chars_of_serial(serial, shape) >= _SHAPE_MIN_OWN_CHARS:
            shapes.add(shape)
    if not shapes:
        return text
    # Longest first, since the shape of one serial can hold another's.
    known = sorted(shapes, key=len, reverse=True)
    pieces, longest = _shape_pieces({_fold(shape) for shape in shapes})

    def mask(match: re.Match) -> str:
        return match.group("head") + _mask_one_shape(
            match.group("shape"), known, pieces, longest,
        )

    return _SHAPE_NOTE_LINE.sub(mask, text)


def _redact(text: str, serials: Iterable[str]) -> str:
    # Read once: the rules below each walk the serials.
    serials = sorted(serials, key=len, reverse=True)
    # The token rule first: masking a short serial inside a 32-hex token
    # would leave the rest of that token, which this rule is there to catch.
    text = _SERIAL_TOKEN.sub(REDACTED, text)
    # Then the shapes, before any serial is masked as itself: one serial's
    # own characters can read as a piece of another's shape (two bridge
    # serials of the registry's choosing), and masking that piece first
    # would leave the rest of the shape with no shape to match.
    text = _mask_serial_shapes(text, serials)
    for serial in serials:
        if len(serial) < _SERIAL_MIN_CHARS:
            # Short enough to read as an ordinary number or an ordinary
            # word, so it is masked only where it stands alone and not
            # inside a longer one: a bridge serial is 1 to 64 characters of
            # the registry's choosing (resources.py), and masking every `ab`
            # would leave `<serial>out the l<serial>el`. A letter or digit
            # on either side is what makes it part of something longer, so a
            # short serial is still masked inside a device path, where `_`,
            # `-` and `.` stand around it.
            boundary = r"\d" if serial.isdigit() else "[0-9A-Za-z]"
            text = re.sub(
                rf"(?<!{boundary}){re.escape(serial)}(?!{boundary})",
                lambda _match: REDACTED, text,
                flags=0 if serial.isdigit() else re.IGNORECASE,
            )
        else:
            # Anywhere it appears, which is how the serial inside a device
            # path is masked: a probe's name on macOS
            # (/dev/cu.usbmodem<serial><interface>) puts a digit right after
            # it. A serial this long inside a longer number would be masked
            # too, which costs a reader nothing. In either case, because a
            # tool may print in the case it likes and the token rule above
            # reaches only a serial standing on its own.
            text = re.sub(re.escape(serial), lambda _match: REDACTED, text,
                          flags=re.IGNORECASE)
    # Last, so a whole serial is masked as one and only the pieces a cut left
    # behind reach this rule.
    return _mask_cut_serials(text, serials)


# A heading of a run's own page, to move down a level when the page becomes
# a section of a report covering several runs. Every heading at once, so a
# section keeps its subsections under it.
_RUN_HEADING = re.compile(r"#{2,5} ")


def _as_a_section(text: str) -> str:
    """One run's page as a section of a report covering several.

    Its title becomes the section heading and every heading it wrote moves a
    level down, so a reader scrolling through two transcripts can see which
    run each belongs to.

    Lines inside a fenced block are left as they are: they are what a
    command printed and what a board answered, quoted, and a reader may copy
    them out (the watchdog's issue body carries `ubertooth-util -w` verbatim).
    A fence closes on a line of at least as many backticks as opened it, so
    the transcript's ``` inside the issue body's ```` does not end it.
    """
    lines = []
    fence = 0
    for line in text.split("\n"):
        bare = line.strip()
        ticks = len(bare) - len(bare.lstrip("`"))
        if fence:
            if ticks >= fence:
                fence = 0
        elif ticks >= 3:
            fence = ticks
        elif _RUN_HEADING.match(line):
            line = "#" + line
        lines.append(line)
    return "\n".join(lines).replace("# Ubertooth test run", "## Run", 1)


def render_run(events: Sequence[Mapping], *, redact_serials: bool = False, transcript: bool = True) -> str:
    run = _Run(events)
    lines = [f"# Ubertooth test run `{run.run_id}`", "", "| | |", "|---|---|"]
    lines += [f"| {_cell(key)} | {_cell(value)} |" for key, value in _setup_rows(run)]
    lines += ["", "## Results", ""] + _results_table(run)
    lines += _known_target_section(run) + _blocked_section(run)
    lines += _not_applicable_section(run)
    lines += _bootloader_section(run)
    lines += _watchdog_section(run)
    lines += _output_files_section(run)
    if transcript:
        lines += ["", "## Transcript", "", LEGEND, "", "```text"] + _transcript(run) + ["```"]
    text = "\n".join(lines) + "\n"
    return _redact(text, run.serials()) if redact_serials else text


def render_runs(paths: Sequence[Path | str], *, redact_serials: bool = False, title: str | None = None) -> str:
    """One report covering several runs (for example macOS and Linux).

    Each run's setup and results, and, when the serials are masked, its
    transcript as well: ``--redact-serials`` means the whole report masked
    (#221), and the transcript is most of what a report is worth to a
    reader. Without masking the page stays a summary, because each run's own
    ``report.md`` already carries its transcript with its serials in it.
    """
    runs = []
    for path in paths:
        path = Path(path)
        run_log = path / RUN_LOG if path.is_dir() else path
        runs.append(_Run(load_events(run_log)))
    lines = [f"# {title or 'Ubertooth validation report'}", "", "| Run | Host | Result |", "|---|---|---|"]
    for run in runs:
        host = run.manifest.get("host") or run.started.get("details", {}).get("host", "")
        lines.append(f"| `{run.run_id}` | {_cell(host)} | {_cell(run.status)} |")
    text = "\n".join(lines) + "\n"
    for run in runs:
        # Not masked per run: the whole page is masked below, with every
        # run's serials, so a board one run names is masked in the other's
        # transcript too.
        text += "\n" + _as_a_section(
            render_run(run.events, transcript=redact_serials)
        )
    serials = set().union(*(run.serials() for run in runs)) if runs else set()
    return _redact(text, serials) if redact_serials else text


# How wide the label column of the console summary is.
_SUMMARY_LABEL = 18
# The rule the console summary is drawn with, the width of a test banner.
_SUMMARY_RULE = "=" * BANNER_WIDTH
# The report's own emphasis, which a terminal would show as the characters
# it is written with. A backtick is left alone: a reason that quotes a
# command or an option carries one, and that is the reason's own text.
_MARKUP = re.compile(r"\*\*")


def _plain(text: str) -> str:
    """One row of the console summary: no markup, and one line.

    A reason comes from a run record and may carry anything, including a
    newline; one would leave the rest of it looking like output nobody
    tagged, under a label that is no longer there.
    """
    return inline(_one_line(_MARKUP.sub("", str(text))))


# The line every host tool prints its own and its libbtbb's version and
# commit on, with or without the label older fork commits leave off
# (preflight.HOST_VERSION_LABEL).
_TOOLS_VERSION_LINE = re.compile(
    r"(?m)^(?:Host tools version: )?libubertooth[ \t].*$"
)


def _host_tools_version(run: _Run) -> str:
    """The version line the run's host tools printed, as they printed it.

    Both versions are on it, libbtbb's second, and the commit in each pair of
    brackets is what that library's own build stamped in: for libbtbb it is
    the only thing that says which library the tools load (#294). It is read
    from the output of preflight's host version check, so what the summary
    shows is what the tools said.

    Where that output is not in the record, the check's own notes are: it
    records every pattern it read and what matched (outputfiles.py), and the
    longest of those matches is as much of the line as it compared. A run
    that built its tools recorded the line in its build, which is the last
    fallback.
    """
    check_id = next(
        (
            str((e.get("details") or {}).get("check_id", ""))
            for e in run.events
            if e["event_type"] == "check_started"
            and (e.get("details") or {}).get("phase") == "preflight"
            and (e.get("details") or {}).get("check_name") == "Host version"
        ),
        "",
    )
    # One string per command and stream: the chunks of each arrive in order,
    # and joining two of them would splice a line of one onto a line of the
    # other. The check asks a second command on an older fork commit, so
    # there can be two.
    streams: dict[tuple[str, str], list[str]] = {}
    for event in run.events:
        details = event.get("details") or {}
        if (
            event["event_type"] != "command_output"
            or not check_id
            or details.get("check_id") != check_id
        ):
            continue
        key = (str(details.get("command_id", "")), str(details.get("stream", "")))
        streams.setdefault(key, []).append(str(details.get("text", "")))
    for pieces in streams.values():
        printed = _TOOLS_VERSION_LINE.search("".join(pieces))
        if printed:
            return printed[0].strip()
    matched = ""
    for event in run.events:
        details = event.get("details") or {}
        if event["event_type"] != CHECKS_EVENT:
            continue
        for check in details.get("checks") or []:
            name = str((check or {}).get("name", ""))
            if name == "Host version" or name.startswith("Host version "):
                found = str((check or {}).get("matched", ""))
                if len(found) > len(matched):
                    matched = found
    if matched:
        return matched
    built = str((run.manifest.get("build") or {}).get("host_version", ""))
    if built:
        return built
    # A `build` command records no manifest, so its own record is what says
    # which version the tools it built report (build.py).
    return next(
        (
            str((e.get("details") or {}).get("host_version", ""))
            for e in reversed(run.events)
            if e["event_type"] == "build_finished"
            and (e.get("details") or {}).get("host_version")
        ),
        "",
    )


def _device_roles(run: _Run) -> dict[str, str]:
    """The hardware roles each alias was bound as, from the manifest."""
    roles: dict[str, list[str]] = {}
    for invocation in run.manifest.get("invocations") or []:
        for role, alias in (invocation.get("resources") or {}).items():
            named = roles.setdefault(str(alias), [])
            if role not in named:
                named.append(str(role))
    return {alias: ", ".join(named) for alias, named in roles.items()}


def _summary_rows(run: _Run) -> list[tuple[str, str]]:
    manifest = run.manifest
    started = run.started.get("details", {})
    rows = [("run", run.run_id)]
    if run.events:
        rows.append((
            "started (UTC)",
            f"{run.events[0]['timestamp']} "
            f"(ran {_duration(run.events[0], run.events[-1])})",
        ))
    plan = manifest.get("plan_name") or started.get("plan_name", "")
    suite = manifest.get("suite") or started.get("suite", "")
    if plan or suite:
        # A failed `build` command has no plan: its record is a run of its
        # own, and a row saying " / " would say nothing.
        rows.append(("plan / suite", f"{plan} / {suite}"))
    environment = manifest.get("environment", {})
    os_info = environment.get("os", {})
    host = manifest.get("host") or started.get("host", "")
    platform = " ".join(
        str(item) for item in (
            os_info.get("system"),
            os_info.get("macos_version") or os_info.get("distribution"),
            os_info.get("release"),
            os_info.get("architecture"),
        ) if item
    )
    if host or platform:
        rows.append(("host", f"{host} ({platform})" if platform else host))
    for source in manifest.get("sources") or started.get("sources", []):
        # The name the commit was chosen by, then the commit itself in full:
        # a tailed run has to identify exactly what was tested.
        ref_name = source.get("ref_name")
        if ref_name and ref_name != source.get("requested_ref"):
            ref = f"{ref_name} at {source.get('commit')}"
        else:
            ref = f"{source.get('requested_ref')} = {source.get('commit')}"
        rows.append((
            str(source.get("name", "source")),
            f"{ref} ({source.get('expected_revision')})",
        ))
    tools = _host_tools_version(run)
    if tools:
        rows.append(("host tools", tools))
    loaded = _libbtbb_in_the_tools(run)
    if loaded:
        rows.append(("libbtbb in tools", _plain(loaded)))
    roles = _device_roles(run)
    for alias, device in _devices(run).items():
        where = roles.get(alias)
        rows.append((
            f"device {alias}",
            (f"{where}, " if where else "")
            + f"serial {device['serial']}"
            + (f": firmware {device['firmware']}" if device["firmware"] else ""),
        ))
    summary = run.finished["details"] if run.finished else {}
    if summary:
        # A run closed before it reached preflight records no counts at all
        # (cli._finish_failed_run), and `0/0 checks passed` would read as a
        # run that asked for none, so those rows are left out instead.
        if "requested_preflight_checks" in summary:
            rows.append((
                "preflight",
                f"{summary.get('completed_preflight_checks', 0)}/"
                f"{summary['requested_preflight_checks']} checks passed",
            ))
        if "requested_tests" in summary:
            rows.append((
                "tests",
                f"{summary.get('completed_tests', 0)}/"
                f"{summary['requested_tests']} passed",
            ))
        counted = (
            ("skipped_cases", "skipped case", "skipped cases"),
            ("not_applicable_cases", "case not applicable", "cases not applicable"),
            ("not_applicable_tests", "test not applicable", "tests not applicable"),
            ("blocked_tests", "test blocked", "tests blocked"),
            ("watchdog_resets", "board self-reset", "board self-resets"),
        )
        also = (count_text(summary.get(key), one, many)
                for key, one, many in counted)
        also = [count for count in also if count]
        if also:
            rows.append(("also", ", ".join(also)))
        if summary.get("failure_reason"):
            rows.append(("reason", _plain(summary["failure_reason"])))
    return rows


def console_summary(events: Sequence[Mapping], *, report: Path | str | None = None) -> str:
    """The block a tailed ``run-suite`` ends with (#342).

    Everything a reader needs to say what was tested and how it went without
    opening ``report.md``: when the run started in UTC, the ref and exact
    commit of each source, the version line the host tools printed, the
    firmware each device was verified on with its alias and serial, the
    totals, and the verdict. It is compiled from the same records the report
    is, so the two cannot disagree.
    """
    run = _Run(list(events))
    rows = _summary_rows(run)
    if report is not None:
        rows.append(("report", str(report)))
    # Wide enough for the longest label this run has, so a long device alias
    # does not cost the block the alignment it is read by.
    width = max([_SUMMARY_LABEL, *(len(label) for label, _value in rows)])
    lines = [_SUMMARY_RULE, "RUN SUMMARY", _SUMMARY_RULE]
    lines.extend(f"  {label:<{width}} {value}" for label, value in rows)
    lines.extend([_SUMMARY_RULE, f"VERDICT: {verdict(run.status)}", _SUMMARY_RULE])
    return "\n".join(lines) + "\n"


def write_console_summary(output_dir: Path | str, stream=None) -> None:
    """Print the console summary of a finished run; never raises.

    The run's result stands whatever happens here, and the terminal may be
    gone already (a dropped SSH session), so every failure is swallowed:
    ``run.jsonl`` and ``report.md`` hold the same facts.
    """
    out = stream if stream is not None else sys.stderr
    try:
        run_log = Path(output_dir) / RUN_LOG
        if not run_log.exists():
            return
        events = load_events(run_log)
        if not events:
            # Nothing was recorded (an empty or unreadable first line): there
            # is nothing to identify the run by, so say nothing.
            return
        # Named only when it is there: write_report warns and returns None
        # when it could not write one.
        written = Path(output_dir) / REPORT
        out.write("\n" + console_summary(
            events, report=written if written.exists() else None
        ))
        out.flush()
    except Exception:  # noqa: BLE001 - a summary must not mask the run result
        pass


def write_report(output_dir: Path | str) -> Path | None:
    """Render ``report.md`` for a run; never raises (the run result stands)."""
    output_dir = Path(output_dir)
    run_log = output_dir / RUN_LOG
    destination = output_dir / REPORT
    try:
        if not run_log.exists():
            return None
        text = render_run(load_events(run_log))
        temporary = destination.with_suffix(".md.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return destination
    except Exception as error:  # noqa: BLE001 - a report error must not mask the run result
        print(f"warning: could not write {destination}: {error}", file=sys.stderr)
        return None
