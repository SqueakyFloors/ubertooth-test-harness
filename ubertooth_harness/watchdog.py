"""Read the firmware's watchdog record and report a board that reset itself.

The fork's firmware resets a board that stops servicing USB, or whose mode
stops looping, and keeps the reason in a corner of RAM a reset does not clear.
`ubertooth-util -U <serial> -w` reads that record: the last boot cause, the
unrequested resets since power-on with the mode and wait the last one happened
in, the capture transfer errors, the last processor fault, and the radio waits
that gave up.

A self-reset is the watchdog doing its job, so the run reports it and does not
fail on it. What it points to -- why the mode stopped answering -- belongs on
the fork, so the report also carries a ready-to-file issue and whatever open
fork issue looks like the same one.

The counter, not the boot cause, is the signal. A reset the host asked for
(`-r`, or entering the bootloader) is a watchdog reset as far as the chip is
concerned, so `Last boot cause: watchdog` appears on a healthy board; the
firmware leaves those out of the count instead. Two reads of a lab board two
minutes apart showed exactly that: the boot count rose by one while the
unrequested count stayed at three. So the warning follows the count, and a
boot cause that gained `watchdog` with no counter increase is recorded as a
note.

Both counters are since power-on. Cutting power to the board (unplugging it,
or switching its hub port) starts them again, and so does flashing firmware
that does not recognize the record it finds: the comparison sees a count that
went backwards, or a boot cause that has gained power-on or brown-out, and
re-baselines the board rather than warning.

What fails a run is a board that does not answer. A record that printed is the
read, whatever became of the rest of the command, because the board that
printed it answered; a read with no record is tried again, since a board the
watchdog has just reset refuses every request until the host notices it
re-enumerated. The last try decides: a board still refusing the request there
is firmware without it, which reads as "watchdog not reported" and is no
failure, and anything else is a board the run cannot use again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import subprocess
import time
from typing import Callable, Mapping, Sequence


# The fork repository a self-reset is worth an issue on.
FORK_REPOSITORY = "SqueakyFloors/ubertooth"

EVENT = "watchdog"
SUMMARY_EVENT = "watchdog_summary"

# `-w` is two control reads; 10 s covers a board that answers slowly without
# holding the run up for one that has gone.
READ_TIMEOUT_S = 10.0
# How long a long test runs between reads. Reads happen between cases, never
# beside a running command, so a test whose cases are longer than this is read
# as often as its cases allow.
POLL_INTERVAL_S = 180.0
# The recovery window for a board that has left USB: this many reads, with
# this pause between them. A watchdog reset re-enumerates in a few hundred
# milliseconds, and the host can refuse the board for a moment longer while it
# notices, so a few seconds is the whole of it. Counted in tries as well as
# seconds, so the wait ends even where the clock is a test's own.
RECOVERY_TRIES = 3
RECOVERY_PAUSE_S = 2.5
# How long the duplicate search may take, and how many a run may run: the
# searches happen after the last test, and nobody is waiting minutes for them.
SEARCH_TIMEOUT_S = 20.0
MAX_SEARCHES = 3

# The reset counter stops here rather than wrapping (the firmware's capture
# transfer errors and radio waits stop at 65535, which no board has reached).
RESET_CAP = 255

# The radio waits, in the words `-w` prints them with.
WAIT_NAMES = {
    "fs_lock": "synthesizer lock",
    "fs_unlock": "unlock",
    "oscillator": "oscillator",
    "state_machine": "state machine",
    "packet_bytes": "packet bytes",
}

# What a read found.
READ = "read"
NOT_REPORTED = "not reported"
ABSENT = "absent"

# Stage labels, which name the read in the record and the report.
BASELINE = "baseline"
DURING_TEST = "during the test"
AFTER_TEST = "after the test"
RUN_END = "run end"

BEFORE_RUN = "before the run"
# When what a read found happened, which is not the same as when it was read:
# the read after a test reports what happened during that test.
_DURING = {
    BASELINE: BEFORE_RUN,
    DURING_TEST: "during the test",
    AFTER_TEST: "during the test",
    RUN_END: "since the run's last read",
}

_BOOT_CAUSE = re.compile(r"(?m)^Last boot cause: (?P<cause>.+?)\s*$")
_ARMED = re.compile(
    r"(?m)^Watchdog: (?:(?P<not_armed>not armed)|armed; (?P<ms>\d+) ms\b.*?)\s*$"
)
_RESETS = re.compile(
    r"(?m)^Unrequested watchdog resets since power-on: (?P<resets>\d+) "
    r"\(in (?P<boots>\d+) boots?\)\s*$"
)
_LAST_RESET = re.compile(r"(?m)^ {2}the last was (?P<where>.+?)\s*$")
_NOW = re.compile(r"(?m)^Now (?P<where>.+?)\s*$")
_FAULT = re.compile(
    r"(?m)^Last processor fault: status (?P<status>0x[0-9a-fA-F]+), "
    r"(?:the access was at (?P<address>0x[0-9a-fA-F]+)|no address recorded)\s*$"
)
_TRANSFER_ERRORS = re.compile(
    r"(?m)^Capture transfer errors since power-on: (?P<count>\d+)\s*$"
)
_RADIO_WAITS = re.compile(
    r"(?m)^Radio waits that gave up since boot: synthesizer lock (?P<fs_lock>\d+), "
    r"unlock (?P<fs_unlock>\d+), oscillator (?P<oscillator>\d+), "
    r"state machine (?P<state_machine>\d+), packet bytes (?P<packet_bytes>\d+)\s*$"
)
# "in idle mode, not in a wait", or the clause the firmware's own image uses
# when it records no mode at all.
_UNNAMED_MODE = "a mode the board does not report"
_WHERE = re.compile(
    rf"^in (?:{re.escape(_UNNAMED_MODE)}|(?P<mode>.+?) mode), (?P<wait>.+)$"
)
# libubertooth's wording for a board the read could not reach: not on the bus,
# or on it and refusing to be opened. Both are worth another try, because that
# is how a board looks for a moment after the watchdog has reset it: it
# re-enumerates in a few hundred milliseconds, and until the host notices it
# came and went, every request to it is refused (ubertooth-util(1), WATCHDOG).
# Only the wordings a read of one board by serial can print: the selection
# that found nothing, the open that was refused, and the board that left the
# bus mid-request.
_UNREACHABLE = re.compile(
    r"(?m)^(?:error: )?(?:"
    r"no Ubertooth found with serial"
    r"|no Ubertooth is attached"
    r"|could not .*the board is no longer attached"
    r"|.*\bis in use by another program\b"
    r"|claiming the USB interface of\b"
    r"|the Ubertooth did not answer in time\b"
    r")"
    # libubertooth's sink for a libusb error with no wording of its own
    # ("error: No such device (it may have been disconnected) (libusb -4)"),
    # which on a control read means the transport failed. The refusal above
    # carries its code inside "(stalled, libusb -9)", so it is not this.
    r"|^error: .+ \(libusb -?\d+\)$"
)
# The board answered and refused the request itself: the only way firmware
# says it has no such request. Transient while the host has not noticed the
# board re-enumerate after its own reset, and permanent on firmware that
# really has not got it, which a test writing the 2020-12-R1 image leaves on
# a board on purpose. The retries tell the two apart.
# Either of the command's two reads: a fork commit that has the watchdog
# record and not the radio waits prints the record and refuses the second.
_NO_SUCH_REQUEST = re.compile(
    r"(?m)^error: could not .*: the board's firmware does not support it\b"
)


def wait_counts(waits: Mapping[str, int]) -> str:
    """Radio wait counts in the words and order `-w` prints them in."""
    return ", ".join(
        f"{label} {waits[name]}" for name, label in WAIT_NAMES.items()
        if name in waits
    )


def _where(text: str) -> tuple[str | None, str]:
    """A "in <mode> mode, <wait>" clause as (mode, wait).

    The mode is None where the board records none, which the `btbr` image
    does: it runs its own state machine rather than one of the firmware's
    modes.
    """
    match = _WHERE.match(text)
    if match is None:
        return None, text
    return match["mode"], match["wait"]


@dataclass(frozen=True)
class WatchdogRecord:
    """One `ubertooth-util -w` report.

    ``transfer_errors`` and ``radio_waits`` are None when the host tools that
    read the board did not print those lines; a build older than the one that
    added them reports the rest without them.
    """

    boot_cause: str
    resets: int
    boots: int
    armed: bool | None = None
    timeout_ms: int | None = None
    # Where the last unrequested reset happened; absent while the count is 0.
    last_reset_mode: str | None = None
    last_reset_wait: str = ""
    last_reset_reported: bool = False
    # What the board is doing now.
    mode: str | None = None
    wait: str = ""
    fault_status: str | None = None
    fault_address: str | None = None
    transfer_errors: int | None = None
    radio_waits: Mapping[str, int] | None = None

    @property
    def causes(self) -> tuple[str, ...]:
        """The boot causes, which the firmware joins with '+'."""
        if self.boot_cause in ("", "not recorded"):
            return ()
        return tuple(part for part in self.boot_cause.split("+") if part)

    @property
    def fault(self) -> str:
        """The processor fault line's values, or "" when none is recorded."""
        if self.fault_status is None:
            return ""
        where = (
            f"the access was at {self.fault_address}" if self.fault_address
            else "no address recorded"
        )
        return f"status {self.fault_status}, {where}"

    def where(self, mode: str | None, wait: str) -> str:
        """A mode and wait as one phrase, for a message."""
        return (f"in {mode} mode" if mode else f"in {_UNNAMED_MODE}") + f", {wait}"

    @property
    def last_reset_where(self) -> str:
        return self.where(self.last_reset_mode, self.last_reset_wait)

    def to_dict(self) -> dict:
        return {
            "boot_cause": self.boot_cause,
            "armed": self.armed,
            "timeout_ms": self.timeout_ms,
            "resets": self.resets,
            "boots": self.boots,
            "last_reset_mode": self.last_reset_mode,
            "last_reset_wait": self.last_reset_wait,
            "mode": self.mode,
            "wait": self.wait,
            "fault": self.fault,
            "transfer_errors": self.transfer_errors,
            "radio_waits": dict(self.radio_waits) if self.radio_waits else None,
        }


def parse_report(text: str) -> WatchdogRecord | None:
    """One `-w` report from a command's output, or None when there is none.

    The reset count line is what says a report was printed: a read that
    failed prints an `error:` line instead and nothing to parse.
    """
    resets = _RESETS.search(text or "")
    if resets is None:
        return None
    cause = _BOOT_CAUSE.search(text)
    armed = _ARMED.search(text)
    last = _LAST_RESET.search(text)
    now = _NOW.search(text)
    fault = _FAULT.search(text)
    transfer = _TRANSFER_ERRORS.search(text)
    waits = _RADIO_WAITS.search(text)
    last_mode, last_wait = _where(last["where"]) if last else (None, "")
    mode, wait = _where(now["where"]) if now else (None, "")
    return WatchdogRecord(
        boot_cause=cause["cause"] if cause else "",
        resets=int(resets["resets"]),
        boots=int(resets["boots"]),
        armed=None if armed is None else armed["not_armed"] is None,
        timeout_ms=int(armed["ms"]) if armed and armed["ms"] else None,
        last_reset_mode=last_mode,
        last_reset_wait=last_wait,
        last_reset_reported=last is not None,
        mode=mode,
        wait=wait,
        fault_status=fault["status"].lower() if fault else None,
        fault_address=(
            fault["address"].lower() if fault and fault["address"] else None
        ),
        transfer_errors=int(transfer["count"]) if transfer else None,
        radio_waits=(
            {name: int(value) for name, value in waits.groupdict().items()}
            if waits else None
        ),
    )


_STALLED = re.compile(r"(?m)^error: could not read the watchdog record: (?P<why>.+?)\s*$")
_ERROR_LINE = re.compile(r"(?m)^(?:error|warning): (?P<text>.+?)\s*$")


def _first_error(output: str) -> str:
    """The first error or warning line of a read, in the tool's own words."""
    first = _ERROR_LINE.search(output or "")
    return first["text"] if first is not None else "nothing"


def _not_reported_reason(output: str) -> str:
    """Why a board that answered printed no record, in its own words.

    The firmware refuses a request it does not have by stalling it, and
    libubertooth says so; anything else is quoted as the tool put it, so a
    read refused for another reason is not filed under old firmware.
    """
    stalled = _STALLED.search(output or "")
    if stalled is not None:
        return f"watchdog not reported: {stalled['why']}"
    first = _ERROR_LINE.search(output or "")
    if first is not None:
        return f"watchdog not reported: {first['text']}"
    return "watchdog not reported, and the read said nothing about why"


def board_unreachable(text: str) -> bool:
    """Does the output say the read could not reach the board at all?

    That is a board off the bus, or one on it that refused to be opened,
    which is how a board the watchdog has just reset looks for a moment. It
    is not firmware that answered and has no such request.
    """
    return _UNREACHABLE.search(text or "") is not None


@dataclass(frozen=True)
class WatchdogChange:
    """How one read compares with the board's baseline."""

    # The counters started again: power was cut to the board, or the firmware
    # did not recognize the record it found. Everything counted is new.
    restarted: bool = False
    new_resets: int = 0
    new_fault: str = ""
    gained_watchdog_cause: bool = False
    # The board's supply dipped far enough to reset it. Not a warning of its
    # own: the part reports it on an ordinary power-up too, so it is what the
    # restart note says about why the counters started again.
    brown_out: bool = False
    new_transfer_errors: int = 0
    new_radio_waits: Mapping[str, int] = field(default_factory=dict)

    @property
    def reset_itself(self) -> bool:
        return self.new_resets > 0


def _rose(current: int | None, baseline: int | None, restarted: bool) -> int:
    """How much a since-power-on counter rose, or its whole value after a
    restart. Negative differences cannot happen once a restart is seen."""
    if current is None:
        return 0
    if baseline is None or restarted:
        return current
    return max(0, current - baseline)


def compare(baseline: WatchdogRecord | None, current: WatchdogRecord) -> WatchdogChange:
    """Compare a read with the baseline taken before the run.

    A boot count or reset count below the baseline's means the counters
    started again, which cutting power to the board does; everything the
    board counts is then new since that restart.
    """
    # Either a counter went backwards, or the board says it has been powered
    # on since: the part reports power-on and a brown-out on an ordinary
    # power-up, and a power cycle that leaves the counts where they already
    # were (a board at 0 resets and 1 boot) shows nothing else.
    restarted = baseline is not None and (
        current.boots < baseline.boots
        or current.resets < baseline.resets
        or bool(
            {"power-on", "brown-out"} & set(current.causes) - set(baseline.causes)
        )
    )
    fault = current.fault
    if fault and baseline is not None and not restarted and fault == baseline.fault:
        fault = ""
    # The radio waits are since boot, not since power-on, so every reboot
    # clears them: after one, what the board holds is all new. A board whose
    # boot count has stopped at its limit hides a reboot from this, which the
    # baseline notes rather than taking every read for a fresh boot.
    rebooted = (
        restarted
        or baseline is None
        or current.boots != baseline.boots
    )
    waits = {}
    for name, value in (current.radio_waits or {}).items():
        base = (baseline.radio_waits or {}).get(name) if baseline else None
        rose = _rose(value, base, rebooted)
        if rose:
            waits[name] = rose
    return WatchdogChange(
        restarted=restarted,
        new_resets=_rose(current.resets, baseline.resets if baseline else None, restarted),
        new_fault=fault,
        gained_watchdog_cause=(
            "watchdog" in current.causes
            and (baseline is None or "watchdog" not in baseline.causes)
        ),
        brown_out=(
            "brown-out" in current.causes
            and (baseline is None or "brown-out" not in baseline.causes)
        ),
        new_transfer_errors=_rose(
            current.transfer_errors,
            baseline.transfer_errors if baseline else None,
            restarted,
        ),
        new_radio_waits=waits,
    )


def warnings_for(
    label: str, record: WatchdogRecord, change: WatchdogChange, during: str
) -> list[str]:
    """What a read is worth warning about: resets, faults, and lost power.

    ``label`` names the board and ``during`` what it was doing, so each line
    reads on its own in the report and the GUI.
    """
    lines = []
    if change.reset_itself:
        times = "once" if change.new_resets == 1 else f"{change.new_resets} times"
        where = (
            f"; the last was {record.last_reset_where}"
            if record.last_reset_reported else ""
        )
        lines.append(
            f"{label} reset itself {times} {during}{where}. The firmware's "
            "watchdog brought the board back; what stopped answering is worth "
            "an issue on the fork."
        )
    if change.new_fault:
        lines.append(
            f"{label} recorded a processor fault {during}: {change.new_fault}."
        )
    return lines


def notes_for(
    label: str, record: WatchdogRecord, change: WatchdogChange, during: str
) -> list[str]:
    """What a read is worth recording without warning about it."""
    lines = []
    if change.restarted:
        # Two causes, and the record cannot always tell them apart: power was
        # cut to the board, or it was flashed with firmware that does not
        # recognize the record it found and started the counts over.
        why = (
            "it was powered on again" if change.brown_out or "power-on" in record.causes
            else "the board lost power, or it was flashed with firmware that "
                 "started the counts over"
        )
        lines.append(
            f"{label} counters started again {during}: {why}. Its baseline is "
            "now this read."
        )
    elif change.gained_watchdog_cause and not change.reset_itself:
        lines.append(
            f"{label} last boot cause is now watchdog with no change in the "
            "unrequested count, so this was a reset the run asked for."
        )
    if change.reset_itself and record.resets >= RESET_CAP:
        lines.append(
            f"{label} has reached the firmware's limit of {RESET_CAP} recorded "
            "resets, so a later reset will not change the count."
        )
    if change.new_transfer_errors:
        lines.append(
            f"{label} recorded {change.new_transfer_errors} capture transfer "
            f"error(s) {during}; each one costs the packet that was arriving."
        )
    if change.new_radio_waits:
        counts = wait_counts(change.new_radio_waits)
        lines.append(
            f"{label} radio waits gave up {during}: {counts}; what that mode "
            "captured may be incomplete."
        )
    return lines


# ----- the fork issue a self-reset is worth ---------------------------------


@dataclass(frozen=True)
class ForkIssue:
    """A ready-to-file fork issue about one board's self-reset."""

    title: str
    body: str
    mode: str | None
    wait: str
    search_terms: str

    def to_dict(self) -> dict:
        return {
            "repository": FORK_REPOSITORY,
            "title": self.title,
            "body": self.body,
            "mode": self.mode,
            "wait": self.wait,
            "search_terms": self.search_terms,
        }


def search_terms(mode: str | None, wait: str) -> str:
    """The GitHub search for open fork issues about the same mode and wait."""
    return " ".join(("watchdog", f'"{mode or _UNNAMED_MODE}"', f'"{wait}"'))


def fork_issue(
    *,
    serial: str,
    resource: str,
    record: WatchdogRecord,
    resets: int,
    build: str = "",
    test_name: str = "",
    run_id: str = "",
    when: str = "",
    report: str = "",
) -> ForkIssue:
    """The issue to file on the fork for a board that reset itself."""
    mode, wait = record.last_reset_mode, record.last_reset_wait
    where = record.where(mode, wait) if record.last_reset_reported else "in a mode the record does not name"
    title = f"Firmware watchdog reset {where}"
    times = "once" if resets == 1 else f"{resets} times"
    lines = [
        f"A board reset itself {times} during a test harness run: the "
        "firmware's watchdog found it was not servicing USB or its mode was "
        f"not looping, {where}.",
        "",
        "The watchdog brought the board back, so nothing was lost; what it "
        "points to is why that mode stopped answering.",
        "",
        f"- Board serial: `{serial}`" + (f" ({resource})" if resource else ""),
        # One line for both: the run built its firmware and its host tools
        # from the same tree, and preflight checked that revision on the
        # board. A test that writes firmware may have left another image on
        # it, which the harness run record says.
        f"- Firmware and host tools: {build or 'not recorded'}",
        f"- Mode: {mode or _UNNAMED_MODE}",
        f"- Wait: {wait or 'not recorded'}",
        f"- Test: {test_name or 'not recorded'}",
        f"- Harness run: {run_id or 'not recorded'}",
        f"- Time (UTC): {when or 'not recorded'}",
    ]
    if record.fault:
        lines.append(f"- Last processor fault: {record.fault}")
    if report:
        lines += ["", "`ubertooth-util -w` after the test:", "", "```text", report.strip(), "```"]
    return ForkIssue(title, "\n".join(lines), mode, wait, search_terms(mode, wait))


ShellRun = Callable[[Sequence[str], float], tuple[int, str, str]]


def _run_gh(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    result = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout_s, check=False,
        # A gh that decides to prompt must not read the run's own input.
        stdin=subprocess.DEVNULL,
    )
    return result.returncode, result.stdout, result.stderr


def search_argv(terms: str, *, repository: str = FORK_REPOSITORY) -> list[str]:
    return [
        "gh", "issue", "list", "-R", repository, "--state", "open",
        "--search", terms, "--limit", "10", "--json", "number,title,url",
    ]


@dataclass(frozen=True)
class DuplicateSearch:
    """Open fork issues that look like the same self-reset.

    ``error`` says why the search did not run (no `gh`, no network, not
    signed in); the issue is then offered to file with no candidates, never
    withheld.
    """

    terms: str
    matches: tuple[Mapping, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "terms": self.terms,
            "matches": [dict(match) for match in self.matches],
            "error": self.error,
        }


def find_duplicates(
    issue: ForkIssue,
    *,
    run: ShellRun | None = None,
    repository: str = FORK_REPOSITORY,
    timeout_s: float = SEARCH_TIMEOUT_S,
) -> DuplicateSearch:
    """Open fork issues matching the issue's mode and wait, through `gh`."""
    runner = run or _run_gh
    terms = issue.search_terms
    try:
        code, stdout, stderr = runner(search_argv(terms, repository=repository), timeout_s)
    except (OSError, subprocess.SubprocessError) as error:
        return DuplicateSearch(terms, error=f"could not run gh: {error}")
    if code != 0:
        detail = (stderr or stdout or f"exit {code}").strip().splitlines()
        return DuplicateSearch(terms, error=f"gh issue list failed: {detail[-1][:200] if detail else code}")
    try:
        found = json.loads(stdout or "[]")
    except ValueError as error:
        return DuplicateSearch(terms, error=f"gh issue list gave no readable list: {error}")
    if not isinstance(found, list):
        return DuplicateSearch(terms, error="gh issue list gave no readable list")
    matches = tuple(
        {
            "number": item.get("number"),
            "title": str(item.get("title", ""))[:200],
            "url": str(item.get("url", ""))[:300],
        }
        for item in found
        if isinstance(item, Mapping) and item.get("number")
    )
    return DuplicateSearch(terms, matches=matches)


# ----- reading the boards during a run -------------------------------------


@dataclass(frozen=True)
class MonitoredBoard:
    resource: str
    serial: str

    @property
    def label(self) -> str:
        return f"{self.resource} (serial {self.serial})"


@dataclass(frozen=True)
class BoardRead:
    board: MonitoredBoard
    state: str  # READ, NOT_REPORTED, or ABSENT
    stage: str
    record: WatchdogRecord | None = None
    change: WatchdogChange = WatchdogChange()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    reason: str = ""
    output: str = ""
    # Whether an unreachable board fails the run. False while the run is being
    # canceled, and for a board already reported lost.
    fails: bool = True

    @property
    def status(self) -> str:
        if self.state == ABSENT:
            # A read whose failure nobody acts on is a warning, not a failure:
            # a mid-test read, or one taken while the run was being canceled.
            return "failed" if self.fails else "warning"
        if self.warnings:
            return "warning"
        return "passed"

    def details(self) -> dict:
        data = {
            "stage": self.stage,
            "resource": self.board.resource,
            "serial": self.board.serial,
            "state": self.state,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "reason": self.reason,
            "new_resets": self.change.new_resets,
            "counters_restarted": self.change.restarted,
        }
        data.update(self.record.to_dict() if self.record else {})
        return data


@dataclass(frozen=True)
class WatchdogOutcome:
    """What a round of reads found: lines to report, and a failure if any."""

    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    failure: str = ""
    resets: int = 0
    # A board did not come back on USB, so it cannot be used again. The other
    # failure a round can report, a count over the test's limit, leaves every
    # board in a known state.
    board_lost: bool = False
    # Serials of the boards this round gave up on, for the caller that
    # reports them (WatchdogMonitor._reported).
    lost: tuple[str, ...] = ()


class WatchdogMonitor:
    """Read `-w` on every board of a run and report what changed.

    ``run_command`` runs one command and records it in run.jsonl; the
    sequencer passes its own, so every read appears in the run's transcript.
    Reads are read-only and happen between cases, never beside a running
    command.

    Each read becomes the board's next baseline, so one self-reset is reported
    once, and a board that lost power is re-baselined where it restarted.
    """

    def __init__(
        self,
        run_command,
        publisher,
        boards: Sequence[MonitoredBoard],
        *,
        build: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_now: Callable[[], str] | None = None,
        interval_s: float = POLL_INTERVAL_S,
        recovery_tries: int = RECOVERY_TRIES,
        recovery_pause_s: float = RECOVERY_PAUSE_S,
        search: Callable[[ForkIssue], DuplicateSearch] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.run_command = run_command
        self.publisher = publisher
        self.boards = tuple(boards)
        # What the run built and preflight checked on the boards, for an issue
        # the fork can act on.
        self.build = build
        self.clock = clock
        self.sleep = sleep
        self.utc_now = utc_now or (lambda: "")
        self.interval_s = interval_s
        self.recovery_tries = max(1, recovery_tries)
        self.recovery_pause_s = recovery_pause_s
        self.search = search or find_duplicates
        # Whether the run has been cancelled. A read still runs then, because
        # a board that reset itself is worth recording either way, but a board
        # that cannot be reached is not waited for.
        self.cancelled = cancelled or (lambda: False)
        # Per board: the record the next read is compared with, how many
        # resets this run has counted, what the last read found, and the issue
        # to offer for its latest self-reset.
        self._baseline: dict[str, WatchdogRecord] = {}
        self._resets: dict[str, int] = {board.serial: 0 for board in self.boards}
        self._state: dict[str, str] = {}
        self._issues: dict[tuple, ForkIssue] = {}
        self._counted: dict[tuple, int] = {}
        self._lost: set[str] = set()
        # Boards whose firmware has shown it has no such request. A board
        # flashed to an image that does have it leaves the set at its next
        # whole read.
        self._no_request: set[str] = set()
        # Per test: what its mid-test reads found, and how many of them there
        # have been (each read needs a command ID of its own).
        self._pending: dict[str, tuple[tuple[str, ...], tuple[str, ...], int]] = {}
        self._polls: dict[str, int] = {}
        self._test_name = ""
        self._last_read = 0.0
        self._read_any = False

    def lost_power(self, aliases) -> None:
        """Forget the baselines of boards whose power was switched.

        A test's power isolation switches other tests' boards off and on, and
        a board that has been powered on again counts from there. Dropping its
        baseline makes its next read the new one, so nothing it counts is
        charged to whichever test happens to read it next.
        """
        wanted = set(aliases)
        for board in self.boards:
            if board.resource in wanted:
                self._baseline.pop(board.serial, None)

    def _for(self, invocation) -> tuple[MonitoredBoard, ...]:
        """The boards one invocation binds.

        A round during a test reads only those: another test's board may be
        powered off by this one's isolation, and its resets are not this
        test's to count against ``max_watchdog_resets``.
        """
        bound = set(invocation.resources.values())
        return tuple(board for board in self.boards if board.resource in bound)

    # ----- one board -------------------------------------------------------

    def _read(
        self, board: MonitoredBoard, stage: str, owner: Mapping, tag: str,
        fails: bool = True,
    ) -> BoardRead:
        """Read one board, waiting out the recovery window if it has gone.

        One try, with no window, while the run is being canceled and for a
        board the run has already given up on: neither is worth waiting for.
        A board whose firmware is known to have no such request ends the
        window as soon as it refuses the request again, but still gets the
        whole of it when something else is wrong -- the read right after a
        test reflashes it, for example.
        """
        tries = (
            1 if self.cancelled() or board.serial in self._lost
            else self.recovery_tries
        )
        for attempt in range(1, tries + 1):
            result = self.run_command(
                command_id=f"{tag}-{board.serial}-{attempt}",
                argv=("ubertooth-util", "-U", board.serial, "-w"),
                timeout_s=READ_TIMEOUT_S,
                owner=owner,
            )
            output = result.stdout + result.stderr
            record = parse_report(output)
            refused = bool(_NO_SUCH_REQUEST.search(output))
            unreachable = board_unreachable(output)
            # A record that printed is the read, whatever became of the rest
            # of the command: `-w` reads the record and then the radio waits,
            # and a board that printed the record answered. Only the board
            # `-U` selected is ever read, so there is no other board the
            # record could have come from. What else the command said becomes
            # a note on the read.
            if record is not None:
                self._no_request.discard(board.serial)
                partial = ""
                if result.timed_out or result.returncode != 0:
                    # The record is there and the rest of the command is not:
                    # the radio waits it reads second, usually.
                    partial = (
                        "its read did not finish: "
                        + (
                            "the command was stopped at its deadline"
                            if result.timed_out else _first_error(output)
                        )
                    )
                return self._compared(board, stage, record, output, partial)
            if refused and not unreachable and board.serial in self._no_request:
                # Already known to be firmware without the request, and
                # refusing it again: nothing to wait for.
                break
            # Not a whole read, so try again: this is the window a board that
            # has just reset itself needs, where it has re-enumerated and the
            # host refuses every request to it until it notices. A
            # cancellation that arrives inside the window ends it.
            if attempt < tries and not self.cancelled():
                self.sleep(self.recovery_pause_s)
            elif attempt < tries:
                break
        if _NO_SUCH_REQUEST.search(output) and not board_unreachable(output) \
                and parse_report(output) is None:
            # It answered every try and refused the request every time, so the
            # firmware really has not got it. Remembered, so a board running
            # such an image is not waited for at every later read.
            self._no_request.add(board.serial)
            reason = f"{board.label}: {_not_reported_reason(output)}"
            return BoardRead(
                board, NOT_REPORTED, stage, reason=reason, notes=(reason,),
                output=output,
            )
        if self.cancelled():
            # Stopping the run is what the operator asked for, and a board may
            # have gone as part of it; the read is recorded, not failed.
            return BoardRead(
                board, ABSENT, stage,
                reason=(
                    f"{board.label} did not answer its watchdog read while the "
                    "run was being canceled"
                ),
                output=output, fails=False,
            )
        window = (
            f"in {tries} tr{'y' if tries == 1 else 'ies'} over about "
            f"{(tries - 1) * self.recovery_pause_s:g}s"
        )
        if not fails:
            # A mid-test read: the read after the test gives the board the
            # window again and is what fails on it.
            return BoardRead(
                board, ABSENT, stage,
                reason=(
                    f"{board.label} did not answer this read ({window}); the "
                    f"read after the test decides. It said: {_first_error(output)}"
                ),
                output=output, fails=False,
            )
        return BoardRead(
            board, ABSENT, stage,
            reason=(
                f"{board.label} did not answer on USB {window}, so the run "
                "cannot read its watchdog record or use the board again; it "
                "has left the bus, something else is holding it, or its reads "
                f"do not finish. The last read said: {_first_error(output)}"
            ),
            output=output,
        )

    def _compared(
        self, board: MonitoredBoard, stage: str, record: WatchdogRecord, output: str,
        partial: str = "",
    ) -> BoardRead:
        during = _DURING.get(stage, stage)
        if stage == BASELINE or board.serial not in self._baseline:
            # Nothing to compare with: what the counters already hold happened
            # before the run, or before whatever stopped the earlier reads, so
            # it is a note and not this run's count. A read whose board has no
            # baseline becomes one, rather than counting the board's whole
            # since-power-on total as this run's.
            change = WatchdogChange()
            warnings: tuple[str, ...] = ()
            notes = tuple(self._baseline_notes(board, record, stage))
        else:
            change = compare(self._baseline.get(board.serial), record)
            warnings = tuple(warnings_for(board.label, record, change, during))
            notes = tuple(notes_for(board.label, record, change, during))
        if partial:
            notes += (f"{board.label} read its watchdog record, but {partial}.",)
        self._baseline[board.serial] = record
        self._resets[board.serial] = self._resets.get(board.serial, 0) + change.new_resets
        if change.reset_itself:
            # Keyed by what the firmware recorded, so a board that reset in
            # two different modes offers an issue for each, and each counts
            # only the resets recorded in its own mode and wait.
            key = (board.serial, record.last_reset_mode, record.last_reset_wait)
            self._counted[key] = self._counted.get(key, 0) + change.new_resets
            self._issues[key] = fork_issue(
                serial=board.serial,
                resource=board.resource,
                record=record,
                resets=self._counted[key],
                build=self.build,
                test_name=self._test_name,
                run_id=getattr(self.publisher, "run_id", ""),
                when=self.utc_now(),
                report=output,
            )
        return BoardRead(
            board, READ, stage, record=record, change=change,
            warnings=warnings, notes=notes, output=output,
        )

    def _baseline_notes(
        self, board: MonitoredBoard, record: WatchdogRecord, stage: str = BASELINE
    ) -> list[str]:
        """What a baseline is worth saying: resets and faults from before it,
        which no later read should be blamed for."""
        notes = []
        if stage != BASELINE:
            notes.append(
                f"{board.label} had no watchdog baseline to compare with, so "
                f"this read ({stage}) is its baseline; what it counts happened "
                "before now."
            )
        if record.resets:
            where = (
                f"; the last was {record.last_reset_where}"
                if record.last_reset_reported else ""
            )
            notes.append(
                f"{board.label} had already reset itself {record.resets} time(s) "
                f"since it was powered on, before this run{where}."
            )
        if record.fault:
            notes.append(
                f"{board.label} carries a processor fault from before this run: "
                f"{record.fault}."
            )
        if record.armed is False:
            notes.append(f"{board.label} reports its watchdog is not armed.")
        # Said here, once: a counter that has stopped cannot rise, so no later
        # read of this board can see it and there is nothing to warn about.
        if record.resets >= RESET_CAP:
            notes.append(
                f"{board.label} is at the firmware's limit of {RESET_CAP} "
                "recorded resets, so a reset during this run will not change "
                "the count."
            )
        if record.boots >= RESET_CAP:
            notes.append(
                f"{board.label} is at the firmware's limit of {RESET_CAP} "
                "recorded boots, so a restart during this run will not show "
                "in the count, and its radio waits will read as one boot's."
            )
        return notes

    # ----- rounds of reads -------------------------------------------------

    def _round(
        self, stage: str, owner: Mapping, tag: str, boards=None, fails: bool = True
    ) -> WatchdogOutcome:
        warnings: list[str] = []
        notes: list[str] = []
        failures: list[str] = []
        lost: list[str] = []
        resets = 0
        read_any = False
        for board in self.boards if boards is None else boards:
            read_any = True
            read = self._read(board, stage, owner, tag, fails)
            self._state[board.serial] = read.state
            warnings.extend(read.warnings)
            notes.extend(read.notes)
            resets += read.change.new_resets
            if read.state == ABSENT:
                # Reported once: a board the run has already given up on is
                # read again at the end, and says so there without being
                # named a second time as the reason the run failed.
                if read.fails and board.serial not in self._lost:
                    failures.append(read.reason)
                    lost.append(board.serial)
            self.publisher.emit(EVENT, read.status, read.details())
        if read_any:
            # A round that read no board leaves the interval where it was, so
            # the next board test still gets its mid-test read.
            self._last_read = self.clock()
            self._read_any = True
        return WatchdogOutcome(
            tuple(warnings), tuple(notes), "; ".join(failures), resets,
            board_lost=bool(failures), lost=tuple(lost),
        )

    def _reported(self, outcome: WatchdogOutcome) -> WatchdogOutcome:
        """Mark the boards whose loss this round's caller reports.

        Only a round whose failure reaches the run gives a board up: a
        mid-test read's does not, so the read after the test still gives that
        board its recovery window and fails on it.
        """
        self._lost.update(outcome.lost)
        return outcome

    def baseline(self) -> WatchdogOutcome:
        """Read every board before the first test, as the baseline.

        A board that does not answer fails here as it does after a test:
        preflight selected it by serial a moment ago, so one that has gone
        since cannot be used by the tests either.
        """
        return self._reported(
            self._round(BASELINE, {"phase": "watchdog"}, "watchdog-baseline")
        )

    def poll_if_due(self, invocation) -> WatchdogOutcome:
        """Read every board again once the interval has passed, so a long test
        reports a self-reset while it is still running.

        Nothing here fails: a board that has gone is reported by the read
        after the test, which gives it the recovery window first.
        """
        if not self._read_any or self.clock() - self._last_read < self.interval_s:
            return WatchdogOutcome()
        if not self._for(invocation):
            # A test that binds no board reads none, and must not move the
            # interval on: the next board test still needs its mid-test read.
            return WatchdogOutcome()
        key = invocation.invocation_id
        self._test_name = invocation.test_name
        self._polls[key] = self._polls.get(key, 0) + 1
        outcome = self._round(
            DURING_TEST, self._owner(invocation),
            f"{key}-watchdog-during-{self._polls[key]}", self._for(invocation),
            fails=False,
        )
        # Kept for the read after the test, which reports the test's warnings
        # in one place; what this round found is already in its own records.
        warnings, notes, resets = self._pending.get(key, ((), (), 0))
        self._pending[key] = (
            warnings + outcome.warnings, notes + outcome.notes, resets + outcome.resets,
        )
        return WatchdogOutcome(outcome.warnings, outcome.notes, "", outcome.resets)

    def after_test(self, invocation, limit: int | None = None) -> WatchdogOutcome:
        """Read every board after a test, with what the mid-test reads found.

        Two things fail: a board that did not come back, and a test that
        passed its plan's ``max_watchdog_resets``. A self-reset on its own is
        a warning.
        """
        self._test_name = invocation.test_name
        outcome = self._round(
            AFTER_TEST, self._owner(invocation),
            f"{invocation.invocation_id}-watchdog-after", self._for(invocation),
        )
        earlier, earlier_notes, earlier_resets = self._pending.pop(
            invocation.invocation_id, ((), (), 0)
        )
        self._polls.pop(invocation.invocation_id, None)
        resets = earlier_resets + outcome.resets
        failure = outcome.failure
        if not failure and limit is not None and resets > limit:
            times = "once" if resets == 1 else f"{resets} times"
            failure = (
                f"{invocation.test_name}: the boards reset themselves {times}, "
                f"more than this test's max_watchdog_resets ({limit})"
            )
        return self._reported(WatchdogOutcome(
            earlier + outcome.warnings,
            earlier_notes + outcome.notes,
            failure,
            resets,
            board_lost=outcome.board_lost,
            lost=outcome.lost,
        ))

    def at_run_end(self) -> WatchdogOutcome:
        """Read every board once more and record the run's watchdog summary.

        The summary carries the per-board reset count the GUI shows and, for
        every board that reset itself, the issue to file on the fork with
        whatever open fork issue the duplicate search found.
        """
        self._test_name = ""
        outcome = self._reported(
            self._round(RUN_END, {"phase": "watchdog"}, "watchdog-run-end")
        )
        issues = []
        for board in self.boards:
            for key, issue in self._issues.items():
                if key[0] != board.serial:
                    continue
                # No search on a run that was canceled: the operator is
                # waiting for it to stop, not for GitHub. Bounded in number
                # too, so a run with many distinct resets does not spend
                # minutes on searches after its last test.
                if self.cancelled():
                    found = DuplicateSearch(
                        issue.search_terms, error="the run was canceled")
                elif len(issues) >= MAX_SEARCHES:
                    found = DuplicateSearch(
                        issue.search_terms,
                        error=f"only the first {MAX_SEARCHES} of this run's "
                              "resets were searched for",
                    )
                else:
                    found = self.search(issue)
                issues.append({
                    "resource": board.resource,
                    "serial": board.serial,
                    "resets": self._counted.get(key, 0),
                    "issue": issue.to_dict(),
                    "duplicates": found.to_dict(),
                })
        self.publisher.emit(
            SUMMARY_EVENT,
            "failed" if self._lost
            else "warning" if self.total_resets else "passed",
            {
                "boards": [self._board_summary(board) for board in self.boards],
                "resets": sum(self._resets.values()),
                "issues": issues,
            },
        )
        return outcome

    @property
    def total_resets(self) -> int:
        """Times the run's boards reset themselves since their baselines."""
        return sum(self._resets.values())

    def _board_summary(self, board: MonitoredBoard) -> dict:
        record = self._baseline.get(board.serial)
        return {
            "resource": board.resource,
            "serial": board.serial,
            "resets": self._resets.get(board.serial, 0),
            "state": self._state.get(board.serial, NOT_REPORTED),
            "boot_cause": record.boot_cause if record else "",
            "fault": record.fault if record else "",
            "transfer_errors": record.transfer_errors if record else None,
            "radio_waits": (
                dict(record.radio_waits) if record and record.radio_waits else None
            ),
        }

    @staticmethod
    def _owner(invocation) -> dict:
        return {
            "phase": "test",
            "invocation_id": invocation.invocation_id,
            "test_name": invocation.test_name,
        }
