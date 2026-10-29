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

from ubertooth_harness.events import REPORT, RUN_LOG
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.rawlog import NOTE, TAGS, CommandCapture, format_command_record
from ubertooth_harness.watchdog import wait_counts


REDACTED = "<serial>"
LEGEND = (
    "Times are elapsed since the run started (UTC start in Time above). "
    "Tags: `[C]` command sent, `[O]` stdout, `[E]` stderr, `[R]` command result, "
    "`[S]` state step, `[I]` power isolation, `[M]` harness record. "
    "`+++` opens a step and `---` closes it; `===` marks the run, build, "
    "preflight, and each test."
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

    @property
    def status(self) -> str:
        if self.finished is None:
            return "incomplete (no run_finished record)"
        return self.finished["status"]

    def serials(self) -> set[str]:
        """Every device serial the run names: resources, preflight, flash
        guards, and boards seen by the isolation enumeration checks."""
        found = set()

        def add(value):
            if isinstance(value, str) and value:
                found.update((value, value.lower()))

        identities = [
            identity
            for invocation in self.manifest.get("invocations", [])
            for identity in invocation.get("resource_identities", {}).values()
        ]
        for event in self.events:
            details = event.get("details", {})
            if event["event_type"] == "check_started":
                identities.append(details.get("resource_identity", {}))
            elif event["event_type"] == "flash_run_started":
                add(details.get("serial"))
                for serial in (*details.get("serials", []), *details.get("guard_serials", [])):
                    add(serial)
            elif event["event_type"].startswith("isolation_"):
                for serial in details.get("enumerated", []):
                    add(serial)
        for identity in identities:
            for key, value in identity.items():
                if "serial" in key:
                    add(value)
        return found


def _duration(first: Mapping | None, last: Mapping | None) -> str:
    if not first or not last:
        return ""
    seconds = (_time(last) - _time(first)).total_seconds()
    return f"{seconds:.1f}s"


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


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
        device = devices.setdefault(alias, {
            "serial": start.get("resource_identity", {}).get("serial", ""),
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
    held = [item for item in held if item.get("key") != "host-power"]
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
    if artifacts:
        rows.append((
            "Artifacts",
            "; ".join(f"{a['name']} sha256 {a['sha256'][:12]}" for a in artifacts),
        ))
    return rows


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
        "A reset is reported, not failed: the board came back by itself.", "",
        "| Board | Serial | Reset itself | Last boot cause | Watchdog record | "
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


def _results_table(run: _Run) -> list[str]:
    lines = [
        "| Test | Host and resources | Result | Failed cases | Skipped cases "
        "| Not applicable cases | Duration | Reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for record in run.tests:
        started = record["started"]
        details = started["details"]
        finished = record["finished"]
        resources = ", ".join(f"{k}={v}" for k, v in details.get("resources", {}).items())
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
            # A board that reset itself does not fail the test, so its warning
            # would otherwise only be in the Watchdog section. Each one is
            # labelled, so it does not read as part of a failure reason.
            for warning in fd.get("warnings", []):
                reason = f"{reason}; " if reason else ""
                reason += f"warning: {warning}"
        lines.append(
            f"| {_cell(details.get('invocation_id'))} | {_cell(where)} | {_cell(status)} "
            f"| {failed} | {skipped} | {not_applicable} | {_duration(started, finished)} "
            f"| {_cell(reason)} |"
        )
    if not run.tests:
        lines.append("| (no tests started) | | | | | | | |")
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
    """[M] note text for output kept in output/<command id>.log, not run.jsonl."""
    summary = note.get("output_omitted") or {}
    lines = summary.get("lines") or {}
    chars = summary.get("chars") or {}
    counts = ", ".join(
        f"{lines.get(stream, 0)} {stream} lines ({chars.get(stream, 0)} characters)"
        for stream in ("stdout", "stderr")
        if lines.get(stream) or chars.get(stream)
    )
    text = (
        f"full output in {note.get('output_file', '')}; left out of run.jsonl here: "
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


def _command_record(
    started: Mapping, outputs: list[Mapping], finished: Mapping | None, stamp
) -> str:
    sd = started["details"]
    fd = finished["details"] if finished else {}
    # Every command's output records are already capped in run.jsonl
    # (rawlog.RECORD_*, the rest in output/), so the report shows them all.
    stdout = "".join(o["details"]["text"] for o in outputs if o["details"].get("stream") == "stdout")
    stderr = "".join(o["details"]["text"] for o in outputs if o["details"].get("stream") == "stderr")
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
    capture.chunks = [
        (_output_time(o), NOTE, _omitted_text(o["details"]))
        if o["event_type"] == "command_output_omitted"
        else (_output_time(o), o["details"].get("stream", "stdout"), o["details"].get("text", ""))
        for o in outputs
    ]
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
            line(event, f"--- {build_step[0]} EXIT: {build_step[1]} ---")
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
            if details.get("phase") == "build" and build_step is not None and status != "passed":
                build_step[1] = status
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
            open_step = details.get("step_name", details.get("step_id", ""))
            line(event, f"+++ {open_step} +++")
            build_step = [open_step, "passed"]
            continue
        elif kind == "build_finished":
            close_build_step(event)
            line(event, f"=== BUILD EXIT: {status}{_reason(details)} ===")
        elif kind == "preflight_started":
            line(event, "=== PREFLIGHT ===")
        elif kind == "preflight_finished":
            line(event, f"=== PREFLIGHT EXIT: {status}{_reason(details)} ===")
        elif kind == "test_started":
            resources = ", ".join(f"{k}={v}" for k, v in details.get("resources", {}).items())
            suffix = f" ({resources})" if resources else ""
            line(event, f"=== TEST {details.get('invocation_id')}: {details.get('test_name')} on {details.get('host')}{suffix} ===")
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
        line(run.events[-1], f"--- {build_step[0]} EXIT: did not finish ---")
    return out


def _reason(details: Mapping, key: str = "reason") -> str:
    reason = details.get(key) or ""
    return f": {reason}" if reason else ""


# Any standalone 32-hex-digit token is masked, since Ubertooth serials have
# that form (for example an unregistered board named in a failure reason);
# this may also mask an unrelated 32-hex value. Longer hex runs such as git
# SHAs (40) and sha256 digests (64) are left alone.
_SERIAL_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")


def _redact(text: str, serials: Iterable[str]) -> str:
    for serial in sorted(serials, key=len, reverse=True):
        text = text.replace(serial, REDACTED)
    return _SERIAL_TOKEN.sub(REDACTED, text)


def render_run(events: Sequence[Mapping], *, redact_serials: bool = False, transcript: bool = True) -> str:
    run = _Run(events)
    lines = [f"# Ubertooth test run `{run.run_id}`", "", "| | |", "|---|---|"]
    lines += [f"| {_cell(key)} | {_cell(value)} |" for key, value in _setup_rows(run)]
    lines += ["", "## Results", ""] + _results_table(run) + _not_applicable_section(run)
    lines += _watchdog_section(run)
    if transcript:
        lines += ["", "## Transcript", "", LEGEND, "", "```text"] + _transcript(run) + ["```"]
    text = "\n".join(lines) + "\n"
    return _redact(text, run.serials()) if redact_serials else text


def render_runs(paths: Sequence[Path | str], *, redact_serials: bool = False, title: str | None = None) -> str:
    """One report covering several runs (for example macOS and Linux)."""
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
        text += "\n" + render_run(run.events, transcript=False).replace("# Ubertooth test run", "## Run", 1)
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
