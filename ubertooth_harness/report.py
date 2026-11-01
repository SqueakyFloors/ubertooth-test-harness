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
from ubertooth_harness import outputfiles
from ubertooth_harness.outputfiles import output_file_list
from ubertooth_harness.rawlog import (
    NOTE,
    RECORD_OUTPUT_BYTES,
    RECORD_OUTPUT_EVENTS,
    RECORD_TAIL_BYTES,
    RECORD_TAIL_EVENTS,
    TAGS,
    Chunk,
    CommandCapture,
    format_command_record,
    line_shape,
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
    "Tags: `[C]` command sent, `[O]` stdout, `[E]` stderr, `[R]` command result, "
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


# Fewest digits a number needs to be masked wherever it appears rather than
# only where it stands alone (_redact). A SEGGER probe's serial is nine
# digits or more, so this is about the short width of a registry entry like
# `000123`, which the tools write as `123`: masking every "123" inside every
# longer number would mangle the transcript, and a standalone one is masked
# either way. The padded width of that same entry is 12 digits, so it is
# masked everywhere, including inside a device path.
_SERIAL_MIN_DIGITS = 6
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
        # Left out before the run because the host cannot run them
        # (hostneeds.py): neither skipped nor failed.
        self.not_applicable = [
            e.get("details", {}) for e in self.events
            if e["event_type"] == "not_applicable"
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
                f"{summary.get('not_applicable_tests', 0)} tests not applicable on this host"
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
    devices = {}
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
    rows.extend(_placement_rows(run))
    if artifacts:
        rows.append((
            "Artifacts",
            "; ".join(f"{a['name']} sha256 {a['sha256'][:12]}" for a in artifacts),
        ))
    return rows


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


def _not_applicable_section(run: _Run) -> list[str]:
    """What the run left out because the host cannot run it, and why."""
    if not run.not_applicable:
        return []
    lines = [
        "", "## Not applicable", "",
        "Left out before the run because this host cannot run them; "
        "not skipped and not failed.", "",
        "| Test | Case | Host | Reason |", "|---|---|---|---|",
    ]
    for item in run.not_applicable:
        test = item.get("invocation_id") or item.get("test_name", "")
        lines.append(
            f"| {_cell(test)} | {_cell(item.get('case_name') or '(whole test)')} "
            f"| {_cell(item.get('host', ''))} | {_cell(item.get('reason', ''))} |"
        )
    return lines


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
                (fd.get("failed_guard_checks", 0), "guard"),
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
            f"{shape.get('shape', '')}\n"
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
        record += f"{stamp(_time(outputs[-1] if outputs else started))} [R] {tool} did not finish\n"
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
            line(event, f"--- {name} EXIT: {status}{_reason(details)} ---")
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
            text = f"[S] {details.get('stage', 'step')} {status}: {details.get('step_name', '')}"
            if details.get("expected") is not None:
                text += f" (expected {details.get('expected')}, observed {details.get('observed')})"
            elif details.get("observed") is not None:
                text += f" (recorded {details.get('observed')})"
            if details.get("reason"):
                text += f": {details['reason']}"
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
# What is masked is a piece of a shape that a serial's own shape holds. The
# rule looks inside a run of hex letters and N rather than at the run as a
# whole, because the shape arrives written against whatever stood beside the
# serial in the output: a serial reaches a line as `ubertooth<serial>` or
# `/dev/cu.usbmodem<serial>`, and a word that ends in a hex letter would
# otherwise carry the shape past the comparison and leave it readable. Each
# piece is masked where it sits and the rest of the run is left as it is. A
# piece the 200-character shape limit cut is a piece of the serial's shape
# like any other, so a cut shape is masked too.
#
# The run must carry an N, since a serial with no digits in it has its own
# characters as its shape and is masked as a serial. A whole shape of a
# serial the run names is masked however few hex letters it carries, because
# the run naming that serial settles the question by itself, the view
# _mask_cut_serials takes of a piece of a known serial. A piece of one needs
# this many letters, because the letters are what names the board and a piece
# is all the rule has to go on: fewer than that (`NabN`, the shape of `1ab2`)
# sits in the shape of too much ordinary output to mask. A run of hex letters
# with no N of its own is left to _mask_cut_serials and its width rule, for
# the same reason: `cafe` and `dead` are words a tool prints.
_SHAPE_MIN_LETTERS = 4
_SHAPE_TOKEN = re.compile(
    r"(?<![0-9A-Fa-fN])[A-Fa-fN]*N[A-Fa-fN]*(?![0-9A-Fa-fN])"
)
_SHAPE_LETTER = re.compile(r"[A-Fa-f]")


def _shape_letters(token: str) -> int:
    """Hex letters in a shape token, which is what names the board (the N
    standing where a run of digits was says nothing about which board)."""
    return len(_SHAPE_LETTER.findall(token))


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


def _mask_shape_pieces(token: str, pieces: set[str], longest: int) -> str:
    """``token`` with every piece of a known serial's shape masked.

    Read from the left, taking the longest piece the known shapes hold at
    each character: what a shape holds is a shape, so masking it is right,
    and a piece that starts later is shorter. Where no piece wide enough
    starts at this character, the character is kept and the next one tried.
    """
    lowered = token.lower()
    out = []
    index = 0
    while index < len(token):
        end = index
        # Every prefix of a piece of a shape is a piece of that shape, so
        # the longest one is reached by growing until there is no longer a
        # piece there.
        while (end < min(len(token), index + longest)
               and lowered[index:end + 1] in pieces):
            end += 1
        if _shape_letters(lowered[index:end]) >= _SHAPE_MIN_LETTERS:
            out.append(REDACTED)
            index = end
        else:
            out.append(token[index])
            index += 1
    return "".join(out)


def _mask_serial_shapes(text: str, serials: Sequence[str]) -> str:
    """Mask the shape of a line that named a board the run names."""
    shapes = set()
    for serial in serials:
        shape = line_shape(serial).lower()
        # No digits in it, so the shape is the serial itself, already masked.
        if shape == serial.lower():
            continue
        # With no hex letter in it the shape names nothing: a numeric serial
        # shapes to a bare `N`, and masking that would mask every shape.
        if _shape_letters(shape):
            shapes.add(shape)
    if not shapes:
        return text
    # A whole shape first, wherever it sits and however few hex letters it
    # carries: the line it was counted from named a board the run names, and
    # that settles it. Longest first, since the shape of one serial can hold
    # the shape of another, and before the pieces below, because a piece
    # taken at an earlier character could swallow the start of a whole shape
    # that repeats itself and leave a tail too narrow to mask.
    for shape in sorted(shapes, key=len, reverse=True):
        text = re.sub(re.escape(shape), REDACTED, text, flags=re.IGNORECASE)
    pieces, longest = _shape_pieces(shapes)

    def mask(match: re.Match) -> str:
        return _mask_shape_pieces(match.group(0), pieces, longest)

    return _SHAPE_TOKEN.sub(mask, text)


def _redact(text: str, serials: Iterable[str]) -> str:
    # Read once: the rules below each walk the serials.
    serials = sorted(serials, key=len, reverse=True)
    # The token rule first: masking a short serial inside a 32-hex token
    # would leave the rest of that token, which this rule is there to catch.
    text = _SERIAL_TOKEN.sub(REDACTED, text)
    for serial in serials:
        if serial.isdigit() and len(serial) < _SERIAL_MIN_DIGITS:
            # Short enough to read as an ordinary number, so it is masked
            # only where it stands alone and not inside a longer one.
            text = re.sub(rf"(?<!\d){re.escape(serial)}(?!\d)", REDACTED, text)
        else:
            # Anywhere it appears, which is how the serial inside a device
            # path is masked: a probe's name on macOS
            # (/dev/cu.usbmodem<serial><interface>) puts a digit right after
            # it. A serial this long inside a longer number would be masked
            # too, which costs a reader nothing. In either case, because a
            # tool may print in the case it likes and the token rule above
            # reaches only a serial standing on its own.
            text = re.sub(re.escape(serial), REDACTED, text, flags=re.IGNORECASE)
    # Before the cut rule, so a shape whose letters happen to run 8 wide is
    # masked as one shape and not in pieces with its N between them.
    text = _mask_serial_shapes(text, serials)
    # Last, so a whole serial is masked as one and only the pieces a cut left
    # behind reach this rule.
    return _mask_cut_serials(text, serials)


def render_run(events: Sequence[Mapping], *, redact_serials: bool = False, transcript: bool = True) -> str:
    run = _Run(events)
    lines = [f"# Ubertooth test run `{run.run_id}`", "", "| | |", "|---|---|"]
    lines += [f"| {_cell(key)} | {_cell(value)} |" for key, value in _setup_rows(run)]
    lines += ["", "## Results", ""] + _results_table(run)
    lines += _known_target_section(run) + _not_applicable_section(run)
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
        # run's serials, so a board that two runs used is masked in both.
        text += "\n" + render_run(
            run.events, transcript=redact_serials,
        ).replace("# Ubertooth test run", "## Run", 1)
    serials = set().union(*(run.serials() for run in runs)) if runs else set()
    return _redact(text, serials) if redact_serials else text


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
