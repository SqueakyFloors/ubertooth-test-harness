"""Opt-in completion notices by email, sent by the controller (#392).

A long run is watched by nobody: the browser is closed, the tunnel is down,
the laptop is asleep. This module lets the controller send one short email
when a run or a run group has finished, so the next person to look already
knows the verdict.

What it is not: notifications are **off** unless a private configuration file
turns them on, nothing is attached (no logs, no captures, no report), no
credential ever reaches a log, a run record, or the message, and a notice
that cannot be sent is a warning in the controller's log and nothing else. A
notice is never part of a verdict: ``Notifier.send`` does not raise, and the
run's status, record and report are written before it is ever called.

Three rules shape the rest:

**After the report, once per run or group.** The controller calls this only
once a terminal status and its ``report.md`` are on disk, so a message never
points at results that are not there. Each notice is claimed in a durable
ledger beside the run directories, by run ID or group ID, with an atomic
exclusive create: a controller that restarts, a runner that reports one run's
end twice, and two children of one group ending together all end in one
notice. A claim that was still being sent when the process died is left
claimed and reported in the log, because nobody can tell from here whether
that message went out, and a duplicate flood is worse than a missing notice.

**One group, one notice.** A run that belongs to a stored group gets no
notice of its own; the group gets one when every host has finished. A run of
its own is a group of one (``rungroups.implicit_group``) and gets a run
notice, which is what a single-host run from the GUI or the API is.

**Nothing private, nothing guessed.** The message carries the verdict, the
IDs, the immutable source identity (the commits, not only the refs a branch
may have moved), UTC times and duration, plan, suite, totals, the result
directory on the test host, and the loopback GUI link to open after the usual
SSH tunnel. A public URL appears only when a sanitized publication record
(#370) has been written for exactly that run or group; anything missing,
unparsable, or not marked sanitized leaves the URL out.
"""

import asyncio
import dataclasses
import datetime
import email.policy
import email.utils
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat
import subprocess
import time
from typing import Callable, Mapping, Sequence

from ubertooth_harness.durations import human_duration


# Where a private configuration lives by default: outside the repository,
# beside the connection settings scripts/harness-config.sh reads.
CONFIG_ENVIRONMENT = "UBERTOOTH_HARNESS_NOTIFY_CONFIG"
CONFIG_NAME = "notify.yaml"
# The ledger of notices already sent, and the publication records a public
# URL may come from, both beside the run directories in the results store.
LEDGER_DIR = "notifications"
PUBLICATIONS_DIR = "publications"

TRANSPORT_SMTP = "smtp"
TRANSPORT_SENDMAIL = "sendmail"
TRANSPORTS = (TRANSPORT_SMTP, TRANSPORT_SENDMAIL)

TLS_STARTTLS = "starttls"
TLS_IMPLICIT = "tls"
TLS_NONE = "none"
TLS_MODES = (TLS_STARTTLS, TLS_IMPLICIT, TLS_NONE)
# Encryption may be left off only for a relay on this machine's loopback
# address, where there is no network to listen on. A name is not an address
# (it is resolved when the connection is made), so only the literals count.
# Authenticated SMTP always requires TLS, whatever the host.
LOOPBACK_HOSTS = ("127.0.0.1", "::1")

# A credential is read from a variable named for this purpose, so a notice
# cannot pick up whatever a generic PASSWORD in the service environment
# happens to hold.
PASSWORD_ENVIRONMENT_PREFIX = "UBERTOOTH_HARNESS_"

# Bounds. Attempts are small on purpose: a notice is a courtesy, and a
# controller must not spend minutes on one while a bench waits.
MAX_RECIPIENTS = 10
MAX_ATTEMPTS = 5
MAX_RETRY_DELAY_S = 60.0
MAX_TIMEOUT_S = 120.0
MAX_SUBJECT_CHARS = 200
MAX_PREFIX_CHARS = 40
# Text taken from a record (a failure reason, a host name, a path) is data:
# bounded before it reaches a header or a line of the body.
MAX_REASON_CHARS = 500
MAX_VALUE_CHARS = 300
MAX_URL_CHARS = 300
# How many hosts of one group the body lists in full.
MAX_LISTED_HOSTS = 16
# How many stages of one release gate the body lists in full. The one
# configuration has twelve; a longer one is summarized rather than mailed
# in full.
MAX_LISTED_STAGES = 24

# Plain `local@domain`, which is every address a bench needs. No display
# name, no comment, no quoted local part, and so nothing that could carry a
# header separator or a bracket into a header.
_ADDRESS_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[A-Za-z0-9.-]{1,190}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class NotifyConfigError(ValueError):
    """The notification configuration is unusable; notices stay off."""


class NotifyError(RuntimeError):
    """One delivery attempt failed. Never reaches a run's verdict."""


def default_config_path(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_CONFIG_HOME/ubertooth-harness/notify.yaml``, outside the repository."""
    base = environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path("~/.config")
    return root.expanduser() / "ubertooth-harness" / CONFIG_NAME


# ----- configuration -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class NotifyConfig:
    """What a private configuration file says, already validated.

    Holds no credential: the password is read from its named source at send
    time (``resolve_password``), so it is never in a dataclass that something
    might repr into a log.
    """

    enabled: bool = False
    sender: str = ""
    recipients: tuple[str, ...] = ()
    transport: str = TRANSPORT_SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    tls: str = TLS_STARTTLS
    username: str = ""
    password_env: str = ""
    password_file: str = ""
    sendmail_path: str = ""
    # What the client calls itself in EHLO. Named rather than looked up:
    # ``smtplib`` otherwise calls ``socket.getfqdn()``, which depends on
    # whatever DNS the host has and can stall for a minute on a machine whose
    # own name does not resolve (it did on a CI runner, long enough for the
    # relay to time the session out). Naming it also keeps the lab host's own
    # name out of the conversation. Empty means the sender's domain.
    ehlo_name: str = ""
    attempts: int = 3
    retry_delay_s: float = 5.0
    timeout_s: float = 20.0
    subject_prefix: str = "[ubertooth-harness]"
    # The loopback GUI a link points at. Empty means "the controller's own
    # port", which is what a deployed controller uses.
    gui_base_url: str = ""

    @property
    def authenticated(self) -> bool:
        return bool(self.username)

    @property
    def client_name(self) -> str:
        """What to send as the EHLO name: the configured one, or the sender's
        domain, so nothing has to ask the host what it is called."""
        return self.ehlo_name or self.sender.rsplit("@", 1)[-1]

    def describe(self) -> str:
        """One line for the log: where notices go, and never a credential."""
        if not self.enabled:
            return "disabled"
        where = (
            f"{self.transport} {self.smtp_host}:{self.smtp_port} ({self.tls})"
            if self.transport == TRANSPORT_SMTP
            else f"{self.transport} {self.sendmail_path}"
        )
        who = (
            f" as {self.username} (password from "
            f"{self.password_env or self.password_file})"
            if self.authenticated else ""
        )
        return f"{where}{who} to {', '.join(self.recipients)}"


_TOP_KEYS = {
    "enabled", "sender", "recipients", "transport", "smtp", "sendmail",
    "attempts", "retry_delay_s", "timeout_s", "subject_prefix", "gui_base_url",
}
_SMTP_KEYS = {
    "host", "port", "tls", "username", "password_env", "password_file",
    "ehlo_name",
}
_SENDMAIL_KEYS = {"path"}


def _section(data: Mapping, name: str, allowed: set) -> Mapping:
    value = data.get(name, {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise NotifyConfigError(f"{name} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise NotifyConfigError(
            f"{name}: unknown setting(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(allowed))}"
        )
    return value


def _flag(data: Mapping, name: str, default: bool) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise NotifyConfigError(f"{name} must be true or false")
    return value


def _text(data: Mapping, name: str, default: str = "") -> str:
    value = data.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise NotifyConfigError(f"{name} must be text")
    if _CONTROL_RE.search(value):
        raise NotifyConfigError(f"{name} must not contain control characters")
    return value.strip()


def _number(data: Mapping, name: str, default: float, lowest: float, highest: float) -> float:
    value = data.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NotifyConfigError(f"{name} must be a number")
    if not lowest <= float(value) <= highest:
        raise NotifyConfigError(f"{name} must be between {lowest} and {highest}")
    return float(value)


def check_address(value, what: str) -> str:
    """One plain ``local@domain`` address, or refuse it.

    A newline, a comma, a bracket or a display name is refused here rather
    than sanitized: an address list is the one place where another header
    could be smuggled in, and a bench's own addresses are plain.
    """
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value.strip()):
        shown = str(value)[:80].replace("\n", "\\n").replace("\r", "\\r")
        raise NotifyConfigError(
            f"{what} must be one plain address of the form name@example.com "
            f"(no display name, no comma, no newline); got {shown!r}"
        )
    return value.strip()


def check_loopback_url(value: str, what: str) -> str:
    """An ``http://127.0.0.1:PORT`` style base URL, with no path or query.

    The GUI is loopback only, so a link in a notice is too: a URL naming
    another host would invite a reader to open something this harness does
    not serve. A literal address, not a name, for the same reason the
    cleartext relay must be one.
    """
    if len(value) > MAX_URL_CHARS:
        raise NotifyConfigError(f"{what} is too long")
    match = re.fullmatch(r"http://(127\.0\.0\.1|\[::1\])(?::(\d{1,5}))?", value)
    if match is None:
        raise NotifyConfigError(
            f"{what} must be http://127.0.0.1[:PORT] (the GUI listens on "
            "loopback only; reach it through the usual SSH tunnel)"
        )
    if match.group(2) is not None and not 1 <= int(match.group(2)) <= 65535:
        raise NotifyConfigError(f"{what} names an impossible port")
    return value


def _check_private_file(path: Path, what: str) -> None:
    """Refuse a file another local user could read or rewrite."""
    try:
        info = path.stat()
    except OSError as error:
        raise NotifyConfigError(f"{what} {path}: {error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise NotifyConfigError(f"{what} {path} is not a regular file")
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise NotifyConfigError(
            f"{what} {path} is readable or writable by others "
            f"(mode {stat.S_IMODE(info.st_mode):03o}); chmod 600 it"
        )


def _check_sendmail(path_text: str) -> str:
    if not path_text:
        raise NotifyConfigError(
            "sendmail.path is required with transport: sendmail, as an "
            "absolute path (for example /usr/sbin/sendmail)"
        )
    path = Path(path_text)
    if not path.is_absolute():
        raise NotifyConfigError(f"sendmail.path {path} must be absolute")
    try:
        info = path.stat()
    except OSError as error:
        raise NotifyConfigError(f"sendmail.path {path}: {error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise NotifyConfigError(f"sendmail.path {path} is not a regular file")
    if not os.access(path, os.X_OK):
        raise NotifyConfigError(f"sendmail.path {path} is not executable")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise NotifyConfigError(
            f"sendmail.path {path} is writable by others "
            f"(mode {stat.S_IMODE(info.st_mode):03o}); it is run as this user"
        )
    return str(path)


def parse_config(data) -> NotifyConfig:
    """Validate one configuration mapping, or raise ``NotifyConfigError``.

    Unknown settings are refused rather than ignored: a typo in a key that
    turns encryption on must not pass as a default.
    """
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise NotifyConfigError("the configuration must be a mapping")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise NotifyConfigError(
            f"unknown setting(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(_TOP_KEYS))}"
        )
    enabled = _flag(data, "enabled", False)
    smtp = _section(data, "smtp", _SMTP_KEYS)
    sendmail = _section(data, "sendmail", _SENDMAIL_KEYS)
    transport = _text(data, "transport", TRANSPORT_SMTP)
    if transport not in TRANSPORTS:
        raise NotifyConfigError(f"transport must be one of {', '.join(TRANSPORTS)}")
    prefix = _text(data, "subject_prefix", "[ubertooth-harness]")[:MAX_PREFIX_CHARS]
    gui = _text(data, "gui_base_url")
    if gui:
        check_loopback_url(gui, "gui_base_url")
    recipients_raw = data.get("recipients", [])
    if isinstance(recipients_raw, str):
        recipients_raw = [recipients_raw]
    if not isinstance(recipients_raw, (list, tuple)):
        raise NotifyConfigError("recipients must be a list of addresses")
    if len(recipients_raw) > MAX_RECIPIENTS:
        raise NotifyConfigError(f"at most {MAX_RECIPIENTS} recipients")
    recipients = tuple(
        check_address(item, "each recipient") for item in recipients_raw
    )
    sender = _text(data, "sender")
    username = _text(smtp, "username")
    password_env = _text(smtp, "password_env")
    password_file = _text(smtp, "password_file")
    tls = _text(smtp, "tls", TLS_STARTTLS)
    if tls not in TLS_MODES:
        raise NotifyConfigError(f"smtp.tls must be one of {', '.join(TLS_MODES)}")
    host = _text(smtp, "host")
    config = NotifyConfig(
        enabled=enabled,
        sender=check_address(sender, "sender") if (enabled or sender) else "",
        recipients=recipients,
        transport=transport,
        smtp_host=host,
        smtp_port=int(_number(smtp, "port", 587, 1, 65535)),
        tls=tls,
        username=username,
        password_env=password_env,
        password_file=password_file,
        sendmail_path=_text(sendmail, "path"),
        ehlo_name=_text(smtp, "ehlo_name"),
        attempts=int(_number(data, "attempts", 3, 1, MAX_ATTEMPTS)),
        retry_delay_s=_number(data, "retry_delay_s", 5.0, 0.0, MAX_RETRY_DELAY_S),
        timeout_s=_number(data, "timeout_s", 20.0, 1.0, MAX_TIMEOUT_S),
        subject_prefix=prefix,
        gui_base_url=gui,
    )
    if not enabled:
        # Nothing else is required of a configuration that sends nothing, so
        # a half-written one can sit there with enabled: false.
        return config
    if config.ehlo_name and not re.fullmatch(r"[A-Za-z0-9.-]{1,255}", config.ehlo_name):
        raise NotifyConfigError(
            "smtp.ehlo_name must be a host name (letters, digits, dots and "
            "hyphens); it is what the client calls itself in EHLO"
        )
    if not config.recipients:
        raise NotifyConfigError("recipients must name at least one address")
    if config.transport == TRANSPORT_SENDMAIL:
        return dataclasses.replace(
            config, sendmail_path=_check_sendmail(config.sendmail_path)
        )
    if not config.smtp_host:
        raise NotifyConfigError("smtp.host is required with transport: smtp")
    if config.authenticated:
        if config.tls == TLS_NONE:
            raise NotifyConfigError(
                "authenticated SMTP requires smtp.tls: starttls or tls; a "
                "password must not cross an unencrypted connection"
            )
        if bool(password_env) == bool(password_file):
            raise NotifyConfigError(
                "with smtp.username, name exactly one of smtp.password_env "
                "(a purpose-specific environment variable) or "
                "smtp.password_file (a file only you can read)"
            )
    elif password_env or password_file:
        raise NotifyConfigError(
            "smtp.password_env and smtp.password_file need smtp.username"
        )
    if password_env:
        if not _ENVIRONMENT_NAME_RE.fullmatch(password_env):
            raise NotifyConfigError(
                "smtp.password_env must be an upper-case environment variable name"
            )
        if not password_env.startswith(PASSWORD_ENVIRONMENT_PREFIX):
            raise NotifyConfigError(
                f"smtp.password_env must start with {PASSWORD_ENVIRONMENT_PREFIX} "
                "so a notice cannot pick up an unrelated secret from the "
                "service environment"
            )
    if password_file and not Path(password_file).is_absolute():
        raise NotifyConfigError("smtp.password_file must be an absolute path")
    if config.tls == TLS_NONE and config.smtp_host not in LOOPBACK_HOSTS:
        raise NotifyConfigError(
            f"smtp.tls: none is only allowed for a relay on this machine "
            f"({', '.join(LOOPBACK_HOSTS)}); {config.smtp_host} needs "
            "starttls or tls"
        )
    return config


def load_config(path: Path | str | None) -> NotifyConfig:
    """Read a private configuration file; a missing path leaves notices off.

    The file names the recipients, so another local user must not be able to
    rewrite it: one that is group- or world-writable is refused.
    """
    if path is None:
        return NotifyConfig()
    import yaml

    file = Path(path).expanduser()
    try:
        info = file.stat()
    except OSError as error:
        raise NotifyConfigError(f"{file}: {error}") from None
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise NotifyConfigError(
            f"{file} is writable by others (mode {stat.S_IMODE(info.st_mode):03o}); "
            "it says where notices go, so chmod go-w it"
        )
    try:
        with file.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise NotifyConfigError(f"{file}: {error}") from None
    try:
        config = parse_config(raw)
    except NotifyConfigError as error:
        raise NotifyConfigError(f"{file}: {error}") from None
    if config.password_file:
        _check_private_file(Path(config.password_file), "smtp.password_file")
    return config


def resolve_password(
    config: NotifyConfig, environ: Mapping[str, str] = os.environ
) -> str:
    """The SMTP password from its named source, read at send time.

    Never stored on the configuration and never logged; a failure here says
    which source was empty, not what it held.
    """
    if config.password_env:
        value = environ.get(config.password_env, "")
        if not value:
            raise NotifyError(
                f"{config.password_env} is unset or empty, so the SMTP "
                "password is unavailable"
            )
        return value
    path = Path(config.password_file)
    _check_private_file(path, "smtp.password_file")
    try:
        value = path.read_text(encoding="utf-8").strip("\r\n")
    except OSError as error:
        raise NotifyError(f"cannot read smtp.password_file {path}: {error}") from None
    if not value:
        raise NotifyError(f"smtp.password_file {path} is empty")
    return value


# ----- the message ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Notice:
    """One completion notice: what to send, and the key that dedupes it."""

    key: str
    subject: str
    body: str


def header_text(value, limit: int = MAX_VALUE_CHARS) -> str:
    """One line of plain text out of a record: no control characters, bounded.

    Everything that reaches a header or a line of the body passes through
    here. A newline in a failure reason becomes a space rather than a second
    header, and rather than an exception in the middle of a run's end.
    """
    text = _CONTROL_RE.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(limit - 3, 1)].rstrip() + "..."
    return text


def _utc(value) -> str:
    """A recorded timestamp as ``YYYY-MM-DDTHH:MM:SSZ``, or what it said."""
    text = header_text(value, 64)
    if not text:
        return "not recorded"
    moment = _moment(text)
    if moment is None:
        return text
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(value) -> datetime.datetime | None:
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def duration_text(started, finished) -> str:
    """``1h 12m 05.000s`` between two recorded timestamps, or why not."""
    first, last = _moment(started), _moment(finished)
    if first is None or last is None:
        return "not recorded"
    seconds = (last - first).total_seconds()
    if seconds < 0:
        return "not recorded"
    return human_duration(seconds)


def test_totals(events: Sequence[Mapping]) -> dict:
    """Result totals from one run's own records.

    ``test_finished`` statuses counted as the records give them, plus what
    the run's own ``run_finished`` summary says it planned and reached. A run
    that ended before its tests has zeros, which is the truth about it.
    """
    counts: dict[str, int] = {}
    summary: Mapping = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        kind = event.get("event_type")
        if kind == "test_finished":
            status = header_text(event.get("status"), 32) or "unknown"
            counts[status] = counts.get(status, 0) + 1
        elif kind == "run_finished":
            details = event.get("details")
            if isinstance(details, Mapping):
                summary = details
    totals = {"counts": counts}
    for name in ("requested_tests", "completed_tests", "blocked_tests"):
        value = summary.get(name)
        totals[name] = value if isinstance(value, int) and not isinstance(value, bool) else None
    reason = summary.get("failure_reason")
    totals["failure_reason"] = header_text(reason, MAX_REASON_CHARS) if reason else ""
    return totals


def _totals_line(totals: Mapping) -> str:
    counts = totals.get("counts") or {}
    if counts:
        # Passed first, then the rest alphabetically: a reader looks for the
        # failures, and a status this side does not know still shows up.
        order = sorted(counts, key=lambda name: (name != "passed", name))
        text = ", ".join(f"{counts[name]} {name}" for name in order)
    else:
        text = "no test finished"
    requested, completed = totals.get("requested_tests"), totals.get("completed_tests")
    if isinstance(requested, int) and isinstance(completed, int):
        text += f" ({completed} of {requested} planned tests reached)"
    blocked = totals.get("blocked_tests")
    if isinstance(blocked, int) and blocked:
        text += f"; {blocked} blocked (required hardware is not registered)"
    return text


def source_lines(events: Sequence[Mapping]) -> list[str]:
    """The immutable source identity of a run, one line per repository.

    From the run's own manifest (or its ``run_started`` record, for a run
    that failed before the manifest): the commit, which cannot move, beside
    the ref name that chose it, which can.
    """
    sources: Sequence = ()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        if event.get("event_type") == "manifest":
            manifest = details.get("manifest")
            if isinstance(manifest, Mapping) and manifest.get("sources"):
                sources = manifest["sources"]
                break
        if event.get("event_type") == "run_started" and details.get("sources"):
            sources = details["sources"]
    lines = []
    for source in sources if isinstance(sources, (list, tuple)) else ():
        if not isinstance(source, Mapping):
            continue
        name = header_text(source.get("name") or "source", 60)
        commit = header_text(source.get("commit") or "not recorded", 64)
        ref = header_text(
            source.get("ref_name") or source.get("requested_ref") or "", 120
        )
        version = header_text(source.get("build_version") or "", 60)
        text = f"  {name}: {commit}"
        if ref:
            text += f" (ref {ref}"
            text += f", builds as {version})" if version else ")"
        elif version:
            text += f" (builds as {version})"
        repository = header_text(source.get("repository") or "", 200)
        if repository:
            text += f"\n    from {repository}"
        lines.append(text)
    return lines


def digest_events(events: Sequence[Mapping]) -> dict:
    """All one run's records contribute to a notice, in a few hundred bytes.

    A group notice needs each host's totals and the source identity, nothing
    else, so the controller reduces one host's records to this and lets them
    go before reading the next. A group of sixteen hosts therefore holds one
    parsed run log at a time, not sixteen.
    """
    return {"totals": test_totals(events), "sources": source_lines(events)}


def read_publication(
    results_dir: Path | str, identifier: str, log: Callable[[str], None] = print
) -> str | None:
    """The public validation URL recorded for this run or group, or None.

    The record is what a #370 export writes once it has published a
    sanitized bundle: ``<results dir>/publications/<run or group id>.json``
    holding ``{"sanitized": true, "url": "https://..."}``. Everything else
    leaves the URL out, because a link in a notice must never point at a
    bundle nobody has checked: a missing file, a record that is not marked
    sanitized, one naming another run, a URL that is not https, and anything
    unreadable all come back as None, the last three with a warning.
    """
    if not _KEY_RE.fullmatch(identifier or ""):
        return None
    path = Path(results_dir) / PUBLICATIONS_DIR / f"{identifier}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log(f"publication record for {identifier} is unreadable: {error}")
        return None
    if not isinstance(data, Mapping):
        log(f"publication record for {identifier} is not a mapping; no public link")
        return None
    named = data.get("run_id") or data.get("group_id")
    if named is not None and named != identifier:
        log(
            f"publication record for {identifier} names {str(named)[:80]!r}; "
            "no public link"
        )
        return None
    if data.get("sanitized") is not True:
        log(
            f"publication record for {identifier} is not marked sanitized; "
            "no public link"
        )
        return None
    url = data.get("url")
    if (
        not isinstance(url, str) or len(url) > MAX_URL_CHARS
        or not url.startswith("https://") or re.search(r"[\s<>\"]", url)
    ):
        log(
            f"publication record for {identifier} has no plain https URL; "
            "no public link"
        )
        return None
    return url


def _gui_link(gui_base: str, identifier: str, *, kind: str = "run") -> str:
    """The loopback GUI's own link to one run or release gate.

    ``/ui/#run=<run_id>`` for a run or a run group, and
    ``/ui/#gate=<gate_id>`` for a release gate, which is the address its own
    panel opens from (#403). A gate ID in a ``#run=`` link would open the GUI
    selecting a run that does not exist.
    """
    base = gui_base.rstrip("/") if gui_base else ""
    if not base or not _KEY_RE.fullmatch(identifier or "") or kind not in (
        "run", "gate"
    ):
        return ""
    return f"{base}/ui/#{kind}={identifier}"


_CLOSING = (
    "No logs, captures or reports are attached. Open the GUI link above "
    "through the usual SSH tunnel, or read report.md in the result "
    "directory on the test host."
)


def _report_problem(run: Mapping) -> str:
    """Why report.md is short or missing for this run, if it is.

    The controller writes the report before it ends a run, and a notice goes
    out after that; but the write itself can fail, and then the notice must
    not send a reader to a file that is not there.
    """
    detail = header_text(run.get("report_error") or "", MAX_REASON_CHARS)
    if "report" in run and not run["report"]:
        return (
            f"report.md was not written ({detail})" if detail
            else "report.md was not written"
        )
    return f"report.md is short: {detail}" if detail else ""


def run_notice(
    run: Mapping,
    *,
    events: Sequence[Mapping] = (),
    gui_base: str = "",
    results_dir: Path | str | None = None,
    public_url: str | None = None,
    subject_prefix: str = "",
) -> Notice:
    """The notice for one run that has finished (a run of its own, #392)."""
    run_id = header_text(run.get("run_id"), 64)
    status = header_text(run.get("status"), 32) or "unknown"
    plan = header_text(run.get("plan") or "-", 120)
    suite = header_text(run.get("suite") or "-", 120)
    totals = test_totals(events)
    rows = [
        ("Verdict", status),
        ("Run", run_id),
        ("Target host", header_text(run.get("target"), 80)),
        ("Plan / suite", f"{plan} / {suite}"),
        ("Tests", _totals_line(totals)),
        ("Started (UTC)", _utc(run.get("created_at"))),
        ("Finished (UTC)", _utc(run.get("finished_at"))),
        ("Duration", duration_text(run.get("created_at"), run.get("finished_at"))),
    ]
    reason = header_text(run.get("reason") or totals.get("failure_reason") or "",
                         MAX_REASON_CHARS)
    if reason:
        rows.append(("Reason", reason))
    problem = _report_problem(run)
    if problem:
        rows.append(("Report", problem))
    body = _render(
        f"Ubertooth harness run {run_id}: {status}",
        rows,
        source_lines(events),
        _places(run, gui_base, results_dir, public_url),
    )
    return Notice(
        key=f"run-{run_id}",
        subject=_subject(subject_prefix, status, f"run {run_id}", plan, suite),
        body=body,
    )


def group_notice(
    group: Mapping,
    digests: Mapping[str, Mapping],
    *,
    gui_base: str = "",
    results_dir: Path | str | None = None,
    public_url: str | None = None,
    subject_prefix: str = "",
) -> Notice:
    """The notice for a run group whose every host has finished.

    One message for the group, with a block per host: its verdict, totals,
    duration and GUI link, or why that host never ran. ``digests`` maps a
    member's run ID to that run's ``digest_events``, so nothing here holds a
    run log.
    """
    group_id = header_text(group.get("group_id"), 80)
    status = header_text(group.get("status"), 32) or "unknown"
    plan = header_text(group.get("plan") or "-", 120)
    suite = header_text(group.get("suite") or "-", 120)
    members = [
        member for member in (group.get("members") or ()) if isinstance(member, Mapping)
    ]
    hosts = header_text(
        f"{group.get('hosts_started')} of {group.get('hosts_asked')} host(s) ran", 80
    )
    rows = [
        ("Verdict", status),
        ("Run group", group_id),
        ("Hosts", hosts),
        ("Plan / suite", f"{plan} / {suite}"),
        ("Started (UTC)", _utc(group.get("created_at"))),
        ("Finished (UTC)", _utc(group.get("finished_at"))),
        ("Duration", duration_text(group.get("created_at"), group.get("finished_at"))),
    ]
    if group.get("cancel_requested"):
        rows.append(("Cancel asked for", header_text(
            group.get("cancel_reason") or "yes", MAX_REASON_CHARS)))
    sources: list[str] = []
    blocks: list[str] = []
    for member in members[:MAX_LISTED_HOSTS]:
        run = member.get("run") if isinstance(member.get("run"), Mapping) else None
        host = header_text(member.get("host"), 80)
        if run is None:
            blocks.append(
                f"  {host}: no run ({header_text(member.get('error') or 'not started', MAX_REASON_CHARS)})"
            )
            continue
        run_id = header_text(run.get("run_id"), 64)
        digest = digests.get(run_id) or {}
        if not sources:
            # One identity for the whole group: every child was sent the same
            # commit (Controller._run_fields resolves the ref once).
            sources = list(digest.get("sources") or ())
        totals = digest.get("totals") or {}
        text = (
            f"  {host}: {header_text(run.get('status'), 32)} - {_totals_line(totals)}\n"
            f"    run {run_id}, "
            f"{duration_text(run.get('created_at'), run.get('finished_at'))}"
        )
        reason = header_text(run.get("reason") or totals.get("failure_reason") or "",
                             MAX_REASON_CHARS)
        if reason:
            text += f"\n    reason: {reason}"
        problem = _report_problem(run)
        if problem:
            text += f"\n    {problem}"
        link = _gui_link(gui_base, run_id)
        if link:
            text += f"\n    {link}"
        directory = header_text(run.get("runner_run_directory") or "", MAX_VALUE_CHARS)
        if directory:
            text += f"\n    results on the test host: {directory}"
        blocks.append(text)
    if len(members) > MAX_LISTED_HOSTS:
        blocks.append(f"  ... and {len(members) - MAX_LISTED_HOSTS} more host(s)")
    places = []
    if results_dir is not None:
        places.append(f"  controller results: {header_text(results_dir, MAX_VALUE_CHARS)}")
    if public_url:
        places.append(f"  published validation bundle: {public_url}")
    parts = [
        f"Ubertooth harness run group {group_id}: {status}",
        "",
        *(f"{name}: {value}" for name, value in rows),
    ]
    if sources:
        parts += ["", "Source (the commit is the identity; a ref can move):", *sources]
    parts += ["", "Hosts:", *blocks]
    if places:
        parts += ["", "Where to look:", *places]
    parts += ["", _CLOSING]
    return Notice(
        key=f"group-{group_id}",
        subject=_subject(subject_prefix, status, f"group {group_id}", plan, suite),
        body="\n".join(parts) + "\n",
    )


def gate_notice(
    gate: Mapping,
    *,
    gui_base: str = "",
    results_dir: Path | str | None = None,
    public_url: str | None = None,
    subject_prefix: str = "",
) -> Notice:
    """The notice for a release gate whose every stage has settled (#403).

    One message for the whole gate: its verdict, the commit every stage ran,
    how long it took, one line per stage with that stage's verdict, duration
    and run IDs, and the gates this configuration measures nothing for. The
    child groups send none of their own, so a gate of twelve stages is one
    message rather than twelve.
    """
    gate_id = header_text(gate.get("gate_id"), 80)
    status = header_text(gate.get("status"), 32) or "unknown"
    config = header_text(gate.get("config") or "-", 120)
    ref = header_text(gate.get("ref") or "-", 120)
    commit = header_text(gate.get("commit") or "-", 80)
    stages = [
        stage for stage in (gate.get("stages") or ()) if isinstance(stage, Mapping)
    ]
    rows = [
        ("Verdict", status),
        ("Release gate", gate_id),
        ("Configuration", config),
        ("Stages", header_text(
            f"{gate.get('required_passed')} of {gate.get('required_asked')} "
            f"required stages passed, {gate.get('stages_passed')} of "
            f"{gate.get('stages_asked')} in all", 120)),
        ("Started (UTC)", _utc(gate.get("created_at"))),
        ("Finished (UTC)", _utc(gate.get("finished_at"))),
        ("Duration", duration_text(gate.get("created_at"), gate.get("finished_at"))),
    ]
    if gate.get("cancel_requested"):
        rows.append(("Cancel asked for", header_text(
            gate.get("cancel_reason") or "yes", MAX_REASON_CHARS)))
    blocks: list[str] = []
    for stage in stages[:MAX_LISTED_STAGES]:
        name = header_text(stage.get("stage"), 80)
        plan = header_text(stage.get("plan") or "-", 120)
        suite = header_text(stage.get("suite") or "-", 120)
        text = (
            f"  {name}: {header_text(stage.get('status'), 32)}"
            f" - {plan}/{suite}, "
            f"{duration_text(stage.get('started_at'), stage.get('finished_at'))}"
        )
        runs = [run for run in (stage.get("runs") or ()) if isinstance(run, Mapping)]
        if runs:
            text += "\n    " + ", ".join(
                f"{header_text(run.get('host'), 80)} "
                f"{header_text(run.get('run_id') or 'no run', 64)}"
                for run in runs[:MAX_LISTED_HOSTS]
            )
        reason = header_text(stage.get("reason") or "", MAX_REASON_CHARS)
        if reason:
            text += f"\n    reason: {reason}"
        blocks.append(text)
    if len(stages) > MAX_LISTED_STAGES:
        blocks.append(f"  ... and {len(stages) - MAX_LISTED_STAGES} more stage(s)")
    manual = [
        header_text(entry.get("name"), 120)
        for entry in (gate.get("manual_gates") or ())
        if isinstance(entry, Mapping)
    ]
    places = []
    if results_dir is not None:
        places.append(f"  controller results: {header_text(results_dir, MAX_VALUE_CHARS)}")
    link = _gui_link(gui_base, gate_id, kind="gate")
    if link:
        places.append(f"  GUI (through the usual SSH tunnel): {link}")
    if public_url:
        places.append(f"  published validation bundle: {public_url}")
    parts = [
        f"Ubertooth harness release gate {gate_id}: {status}",
        "",
        *(f"{name}: {value}" for name, value in rows),
        "",
        "Source (the commit is the identity; a ref can move):",
        f"  Ubertooth {ref} at {commit}",
        "",
        "Stages:",
        *blocks,
    ]
    if manual:
        parts += [
            "",
            "This gate measures nothing for (run by hand, recorded with "
            "release-evidence --attest):",
            *(f"  {name}" for name in manual),
        ]
    if places:
        parts += ["", "Where to look:", *places]
    parts += ["", _CLOSING]
    return Notice(
        key=f"gate-{gate_id}",
        subject=_subject(subject_prefix, status, f"release gate {gate_id}",
                         config, header_text(ref, 120)),
        body="\n".join(parts) + "\n",
    )


def _places(
    run: Mapping, gui_base: str, results_dir: Path | str | None, public_url: str | None
) -> list[str]:
    places = []
    directory = header_text(run.get("runner_run_directory") or "", MAX_VALUE_CHARS)
    if directory:
        places.append(f"  results on the test host: {directory}")
    if results_dir is not None:
        places.append(f"  controller results: {header_text(results_dir, MAX_VALUE_CHARS)}")
    link = _gui_link(gui_base, header_text(run.get("run_id"), 64))
    if link:
        places.append(f"  GUI (through the usual SSH tunnel): {link}")
    if public_url:
        places.append(f"  published validation bundle: {public_url}")
    return places


def _render(
    title: str, rows: Sequence[tuple[str, str]], sources: Sequence[str],
    places: Sequence[str],
) -> str:
    parts = [title, "", *(f"{name}: {value}" for name, value in rows)]
    if sources:
        parts += ["", "Source (the commit is the identity; a ref can move):", *sources]
    if places:
        parts += ["", "Where to look:", *places]
    parts += ["", _CLOSING]
    return "\n".join(parts) + "\n"


def _subject(prefix: str, status: str, what: str, plan: str, suite: str) -> str:
    text = f"{status.upper()}: {plan}/{suite} {what}"
    if prefix:
        text = f"{prefix} {text}"
    return header_text(text, MAX_SUBJECT_CHARS)


def build_message(
    config: NotifyConfig, notice: Notice, *, now: datetime.datetime | None = None
) -> bytes:
    """One plain-text message, ready for a transport. No attachment, ever.

    Every header value has already been through ``header_text``; the SMTP
    policy refuses a newline in a header as well, so an injected header
    cannot survive either check.
    """
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = check_address(config.sender, "sender")
    message["To"] = ", ".join(
        check_address(item, "each recipient") for item in config.recipients
    )
    message["Subject"] = header_text(notice.subject, MAX_SUBJECT_CHARS)
    message["Date"] = email.utils.format_datetime(
        now or datetime.datetime.now(datetime.timezone.utc)
    )
    message["Message-ID"] = email.utils.make_msgid(
        domain=config.sender.rsplit("@", 1)[-1]
    )
    # So a mail client and an out-of-office rule both know this is a machine
    # talking, not a person.
    message["Auto-Submitted"] = "auto-generated"
    # Plain 7-bit text where it can be, so the message a reader (or a test)
    # sees is the message that was built: quoted-printable would wrap the
    # lines and rewrite every "=" in a link. Anything a record carries that
    # is not ASCII falls back to UTF-8.
    try:
        notice.body.encode("ascii")
    except UnicodeEncodeError:
        message.set_content(notice.body, subtype="plain", charset="utf-8")
    else:
        message.set_content(
            notice.body, subtype="plain", charset="us-ascii", cte="7bit"
        )
    return message.as_bytes()


# ----- transports ----------------------------------------------------------


class SmtpTransport:
    """Deliver through an SMTP relay, with verified TLS wherever it is used."""

    def __init__(
        self, config: NotifyConfig, environ: Mapping[str, str] = os.environ
    ):
        self.config = config
        self.environ = environ

    def describe(self) -> str:
        return f"{self.config.smtp_host}:{self.config.smtp_port} ({self.config.tls})"

    def deliver(self, sender: str, recipients: Sequence[str], data: bytes) -> None:
        config = self.config
        password = resolve_password(config, self.environ) if config.authenticated else ""
        # The default context verifies the chain and the host name, and there
        # is no setting here that turns either off.
        context = ssl.create_default_context() if config.tls != TLS_NONE else None
        try:
            if config.tls == TLS_IMPLICIT:
                client = smtplib.SMTP_SSL(
                    config.smtp_host, config.smtp_port,
                    local_hostname=config.client_name,
                    timeout=config.timeout_s, context=context,
                )
            else:
                client = smtplib.SMTP(
                    config.smtp_host, config.smtp_port,
                    local_hostname=config.client_name,
                    timeout=config.timeout_s,
                )
            with client:
                client.ehlo()
                if config.tls == TLS_STARTTLS:
                    client.starttls(context=context)
                    client.ehlo()
                if config.authenticated:
                    client.login(config.username, password)
                refused = client.sendmail(sender, list(recipients), data)
            if refused:
                raise NotifyError(
                    "the relay refused " + ", ".join(sorted(refused))
                )
        except NotifyError:
            raise
        except (OSError, smtplib.SMTPException) as error:
            # The message names the relay and the kind of failure; a password
            # is never part of an smtplib message, and none is added here.
            raise NotifyError(
                f"SMTP {self.describe()}: {type(error).__name__}: {error}"
            ) from None


class SendmailTransport:
    """Deliver through a local sendmail-compatible binary, by argv and stdin.

    No shell: the message goes in on standard input and the addresses are
    arguments past ``--``, each already validated as a plain address, so
    nothing a record carries can become part of a command.
    """

    def __init__(self, config: NotifyConfig):
        self.config = config

    def describe(self) -> str:
        return self.config.sendmail_path

    def deliver(self, sender: str, recipients: Sequence[str], data: bytes) -> None:
        argv = [
            self.config.sendmail_path, "-i", "-f", sender, "--", *recipients
        ]
        try:
            finished = subprocess.run(  # noqa: S603 - absolute path, no shell
                argv,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.timeout_s,
                shell=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise NotifyError(
                f"{self.describe()}: {type(error).__name__}: {error}"
            ) from None
        if finished.returncode != 0:
            detail = header_text(
                finished.stderr.decode("utf-8", "replace")
                or finished.stdout.decode("utf-8", "replace"),
                MAX_REASON_CHARS,
            )
            raise NotifyError(
                f"{self.describe()} exited {finished.returncode}: {detail}"
            )


def build_transport(
    config: NotifyConfig, environ: Mapping[str, str] = os.environ
):
    if config.transport == TRANSPORT_SENDMAIL:
        return SendmailTransport(config)
    return SmtpTransport(config, environ)


# ----- the notifier --------------------------------------------------------


SENDING = "sending"
SENT = "sent"
FAILED = "failed"


class Notifier:
    """Sends each notice at most once, retries a little, and never raises.

    The ledger is one small JSON file per notice in ``ledger_dir``, named by
    the run or group ID. A notice is claimed with an exclusive create before
    the first attempt, so a restart, a replayed run end and two children of
    one group finishing together cannot produce two messages; the claim is
    rewritten as ``sent`` or ``failed`` when the attempts are over. A claim
    left as ``sending`` by a process that died is not retried: from here
    nobody can tell whether that message went out.
    """

    def __init__(
        self,
        config: NotifyConfig,
        ledger_dir: Path | str | None = None,
        *,
        log: Callable[[str], None] = print,
        transport=None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime.datetime] | None = None,
    ):
        self.config = config
        # None means "no ledger": nothing is deduplicated, which is what the
        # test-email command wants and what no run ever uses.
        self.ledger_dir = Path(ledger_dir) if ledger_dir is not None else None
        self.log = log
        self.transport = transport if transport is not None else build_transport(config)
        self.sleep = sleep
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _path(self, key: str) -> Path:
        if self.ledger_dir is None:
            raise NotifyError("this notifier keeps no ledger")
        if not _KEY_RE.fullmatch(key or ""):
            raise NotifyError(f"notice key {key[:80]!r} is not a plain identifier")
        return self.ledger_dir / f"{key}.json"

    def state(self, key: str) -> str | None:
        """What the ledger says about this notice, or None if it is unclaimed."""
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, NotifyError):
            return None
        except (OSError, ValueError):
            # A claim that cannot be read is still a claim: something wrote
            # it, so nothing is sent again under that key.
            return SENDING
        return data.get("state") if isinstance(data, Mapping) else SENDING

    def _claim(self, key: str) -> bool:
        """Claim this notice, or report that it is already claimed."""
        path = self._path(key)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        record = json.dumps(
            {"state": SENDING, "claimed_at": self.now().isoformat(timespec="seconds")},
            sort_keys=True,
        ) + "\n"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(record)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def _record(self, key: str, state: str, detail: str = "", attempts: int = 0) -> None:
        path = self._path(key)
        temporary = path.with_suffix(".json.tmp")
        data = {
            "state": state,
            "attempts": attempts,
            "recorded_at": self.now().isoformat(timespec="seconds"),
        }
        if detail:
            data["detail"] = header_text(detail, MAX_REASON_CHARS)
        try:
            temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as error:
            # The claim stands, which is what keeps the notice unique; only
            # its outcome could not be written down.
            self.log(f"warning: could not record notice {key} as {state}: {error}")

    def send(self, notice: Notice, *, deduplicate: bool = True) -> bool:
        """Send one notice; returns whether it went out. Never raises.

        Blocking: run it off the event loop (``deliver``). A failure is a
        warning in the log, never a change to any run's verdict.
        """
        if not self.config.enabled:
            return False
        deduplicate = deduplicate and self.ledger_dir is not None
        try:
            if deduplicate and not self._claim(notice.key):
                state = self.state(notice.key)
                if state == SENDING:
                    self.log(
                        f"notice {notice.key} was claimed by a run of this "
                        "controller that did not finish sending it; not sending "
                        "it again"
                    )
                return False
        except (NotifyError, OSError) as error:
            self.log(f"warning: cannot claim notice {notice.key}: {error}")
            return False
        attempts = max(1, min(self.config.attempts, MAX_ATTEMPTS))
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                data = build_message(self.config, notice, now=self.now())
                self.transport.deliver(
                    self.config.sender, self.config.recipients, data
                )
            except Exception as error:  # noqa: BLE001 - a notice never fails a run
                last = f"{type(error).__name__}: {error}"
                self.log(
                    f"warning: notice {notice.key} attempt {attempt} of "
                    f"{attempts} failed: {last}"
                )
                if attempt < attempts and self.config.retry_delay_s > 0:
                    self.sleep(self.config.retry_delay_s)
                continue
            if deduplicate:
                self._record(notice.key, SENT, attempts=attempt)
            self.log(f"notice {notice.key} sent to {', '.join(self.config.recipients)}")
            return True
        self.log(
            f"warning: notice {notice.key} was not sent after {attempts} "
            f"attempt(s); the run's own result is unaffected. Last failure: {last}"
        )
        if deduplicate:
            self._record(notice.key, FAILED, last, attempts=attempts)
        return False

    async def deliver(self, notice: Notice) -> bool:
        """``send`` in a worker thread, so the controller's loop keeps serving."""
        try:
            return await asyncio.to_thread(self.send, notice)
        except Exception as error:  # noqa: BLE001 - a notice never fails a run
            self.log(f"warning: notice {notice.key} could not be sent: {error}")
            return False


def sample_notice(subject_prefix: str = "", gui_base: str = "") -> Notice:
    """A fixed notice for the test-email command: no real run, no credential."""
    run = {
        "run_id": "20260101T000000Z-000000000000",
        "status": "passed",
        "target": "Host_Example",
        "plan": "fast",
        "suite": "fast",
        "created_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:04:12+00:00",
        "runner_run_directory": "<the run directory on the test host>",
    }
    events = [
        {"event_type": "test_finished", "status": "passed"},
        {
            "event_type": "run_finished",
            "status": "passed",
            "details": {"requested_tests": 1, "completed_tests": 1},
        },
    ]
    notice = run_notice(
        run, events=events, gui_base=gui_base, subject_prefix=subject_prefix
    )
    return Notice(
        key="test-email",
        subject=notice.subject.replace("PASSED:", "TEST:"),
        body=(
            "This is a test notice from the Ubertooth test harness. It is not "
            "a real run.\n\n" + notice.body
        ),
    )
