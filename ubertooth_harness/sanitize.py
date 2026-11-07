"""Turning run records into text that may be published, and refusing the rest.

A run is recorded for the bench it ran on: the transcript carries the paths
the commands ran with, the home directory the tools were built in, the lease
holder's user name and machine name, the registry's own aliases for the host
and every board, and each device's serial. None of that may go out with a
release (#370), and this repository's own rule is stricter than most: never
name a lab machine, a lab user, or another system in the lab.

So the export has two halves, and the second one is the gate:

* :func:`sanitize` rewrites one line at a time. It knows two kinds of thing
  to take out. **Named** ones are read off the run's own records -- the
  serials, the lease holders' user and machine names, the registry aliases --
  and are replaced by a public label, so nothing has to be typed in by hand
  and a bench that registers its host as its host name is covered like any
  other. **Structural** ones are recognized by their form: a home directory,
  a temporary build directory, an electronic mail address, an IP address, a
  name in a private domain.
* :func:`findings` then reads the sanitized line again and reports anything
  that still looks private. It is a denylist over the finished text rather
  than a second guess at the input, which is what makes it a gate: a rule
  :func:`sanitize` does not implement, or implements wrongly, shows up here
  as a refusal to publish rather than as a leak. :func:`require_safe` turns
  findings into :class:`Unsafe`, and the export writes nothing past that
  point.

The cost of a gate that fails closed is a false positive, and the one to know
about is the absolute path: every root a host tool legitimately names is
allowed (:data:`SYSTEM_ROOTS`), and any other absolute path is a finding, on
the view that a path this does not recognize is more likely a bench's own
directory than something a reader needs. ``--strip-path`` is how an operator
answers one: it names the prefix, which :func:`sanitize` then replaces before
the scan ever sees it.

What is deliberately kept: public repository URLs, release names, commits,
tool and firmware versions, plan and suite names, timestamps, durations,
verdicts, and measurements. Those are the evidence; the rest is the bench.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import ipaddress
import re

from ubertooth_harness.report import REDACTED, redact_serials


# What each kind of private thing becomes. They carry their angle brackets so
# a reader can tell a placeholder from a name, and so the scan below can tell
# one from a path it has to judge: every placeholder ends in ``>``.
HOME = "<home>"
TMP = "<tmp>"
# A prefix an operator named with ``--strip-path``, and the source checkouts
# and run directory a run records of its own. Not ``<home>``: a bench
# directory is not always under one, and a reader should be able to tell the
# two apart.
PATH = "<path>"
USER = "<user>"
HOST = "<host>"
EMAIL = "<email>"
ADDRESS = "<address>"
# A device serial is masked by report.py, which knows every width a run
# writes one in; the placeholder is its.
SERIAL = REDACTED

PLACEHOLDERS = (HOME, TMP, PATH, USER, HOST, EMAIL, ADDRESS, SERIAL)


class Unsafe(ValueError):
    """Sanitized text still carries something that must not be published."""


@dataclass(frozen=True)
class Finding:
    """One thing the scan will not publish, and where it is."""

    rule: str
    where: str
    line: int
    text: str

    def describe(self) -> str:
        return f"{self.where}:{self.line}: {self.rule}: {self.text}"


@dataclass(frozen=True)
class Private:
    """The names one export takes out, gathered from the runs it covers.

    ``aliases`` maps a registry alias to the public label that replaces it,
    so a reader of the bundle sees ``Ubertooth 1`` where the bench says
    whatever its registry says. ``paths`` are absolute prefixes an operator
    named with ``--strip-path``, replaced before anything else so a bench
    directory this module would not recognize is still taken out.

    Every field is a name to find and remove. Nothing here is published:
    :func:`evidence.build_bundle` keeps the mapping in memory for the length
    of one export.
    """

    serials: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    paths: tuple[str, ...] = ()
    # A private bundle keeps the serials (``--private``), so the scan must
    # not read one as a leak. It is a field of this object rather than an
    # argument of every rule, because it is a property of the export: the
    # bundle that keeps serials is the one that is not published.
    keep_serials: bool = False

    def merged(self, other: "Private") -> "Private":
        """Both sets as one, for an export covering several runs."""
        return Private(
            serials=tuple(sorted(set(self.serials) | set(other.serials))),
            users=tuple(sorted(set(self.users) | set(other.users))),
            hosts=tuple(sorted(set(self.hosts) | set(other.hosts))),
            aliases={**self.aliases, **other.aliases},
            paths=tuple(sorted(set(self.paths) | set(other.paths))),
            keep_serials=self.keep_serials or other.keep_serials,
        )

    def with_paths(self, paths: Iterable[str]) -> "Private":
        return replace(self, paths=tuple(sorted(set(self.paths) | set(paths))))

    @property
    def names(self) -> tuple[str, ...]:
        """Every name whose survival in the output is a finding.

        Longest first, so a name that contains another is found as itself.
        The aliases' own labels are not in here: they are what the names
        became.
        """
        found = {*self.users, *self.hosts, *self.aliases, *self.paths}
        return tuple(sorted(found, key=lambda name: (-len(name), name)))


# ---------------------------------------------------------------------------
# Taking the private things out
# ---------------------------------------------------------------------------

# A home directory, as either platform writes one. The user's own name is one
# path segment and is taken with the prefix, so `/home/ada/ubertooth/host`
# becomes `<home>/ubertooth/host`: the operator is gone and the checkout is
# still readable. `/root` has no segment of its own.
_HOME_PATHS = (
    re.compile(r"/Users/[^/\s\"'`)\]},;:|]+"),
    re.compile(r"/home/[^/\s\"'`)\]},;:|]+"),
    re.compile(r"/root(?=/|\b)"),
)
# A temporary directory, where a build or a test wrote something that is not
# evidence. macOS puts a per-user directory under /var/folders (and names it
# /private/var/folders in a resolved path), and both platforms use /tmp.
_TMP_PATHS = (
    re.compile(r"(?:/private)?/var/folders/[^/\s]+/[^/\s]+"),
    re.compile(r"(?:/private)?/tmp(?=/|\b)"),
    # A per-user runtime directory, whose own segment is the operator's
    # numeric user id.
    re.compile(r"/run/user/\d+"),
)
# An electronic mail address: operator data, taken out whole rather than by
# its parts, so the host it names does not survive as `<user>@host`.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# What is left of `ada@lab-host` once the user and the machine have each been
# replaced by their placeholder: the machine alone says as much, and the `@`
# form is what the scan reads as a machine name.
_USER_AT_HOST = re.compile(rf"{re.escape(USER)}@{re.escape(HOST)}")
# A name in a domain nobody outside the site can resolve. These are the
# suffixes a bench reaches its own machines by; a public domain is not here,
# because a public URL is evidence (the repository the commit came from).
PRIVATE_DOMAINS = ("local", "internal", "intranet", "lan", "corp", "home")
_PRIVATE_HOST = re.compile(
    r"(?<![\w.-])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*"
    rf"\.(?:{'|'.join(PRIVATE_DOMAINS)})(?![\w-])"
)
# Four dotted numbers that are a usable address. A version number of four
# parts has the same form and would be replaced too, which is the price of a
# rule that has only the form to go on; the harness records versions of three
# parts, and a transcript that carried one of four would show `<address>`
# where a reader could have had the version.
_DOTTED_QUAD = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
# The addresses a public record may keep: the loopback the controller binds
# and the unspecified address, which say nothing about a bench.
PUBLIC_ADDRESSES = ("127.0.0.1", "0.0.0.0", "255.255.255.255")


def _alias_pattern(alias: str) -> re.Pattern:
    """``alias`` where it stands on its own.

    A registry alias is letters, digits and the separators a registry allows,
    so it is bounded by anything that is not one of those: `UT_0` in
    `serial of UT_0,` is the alias, and the `UT_0` inside `UT_01` is not.
    """
    return re.compile(rf"(?<![0-9A-Za-z_-]){re.escape(alias)}(?![0-9A-Za-z_-])")


def _name_pattern(name: str) -> re.Pattern:
    """A user or machine name where it stands on its own, in any case.

    A machine name reaches a record as its own word (`held by ada on
    lab-host`) and inside a domain name (`lab-host.internal`), so a dot is
    not a boundary: the name is replaced and the domain suffix is left for
    the private-domain rule above to take.
    """
    return re.compile(rf"(?<![0-9A-Za-z_-]){re.escape(name)}(?![0-9A-Za-z_-])",
                      re.IGNORECASE)


def sanitize(text: str, private: Private = Private()) -> str:
    """``text`` with every private thing replaced by its placeholder.

    One line of a transcript is one unit of work and no rule reads across a
    line ending, so a caller may sanitize a file a line at a time however
    long it is.

    The order matters in three places. The operator's named paths go first,
    because a prefix they supplied may sit under a home directory and has to
    win. Mail addresses go before user and machine names, so an address is
    taken out whole. Serials go last, because report.py's rules read the
    pieces an output cap left behind, and a replacement made after them would
    leave a piece with nothing to match.
    """
    for prefix in sorted(private.paths, key=len, reverse=True):
        text = text.replace(prefix.rstrip("/"), PATH)
    for alias, label in sorted(
        private.aliases.items(), key=lambda item: (-len(item[0]), item[0])
    ):
        text = _alias_pattern(alias).sub(label, text)
    text = _EMAIL.sub(EMAIL, text)
    for name in sorted(private.users, key=len, reverse=True):
        text = _name_pattern(name).sub(USER, text)
    for name in sorted(private.hosts, key=len, reverse=True):
        text = _name_pattern(name).sub(HOST, text)
    text = _USER_AT_HOST.sub(HOST, text)
    for pattern in _HOME_PATHS:
        text = pattern.sub(HOME, text)
    for pattern in _TMP_PATHS:
        text = pattern.sub(TMP, text)
    text = _PRIVATE_HOST.sub(HOST, text)
    text = _DOTTED_QUAD.sub(_mask_address, text)
    if private.keep_serials:
        # A private bundle keeps them, and report.py's rules mask a serial by
        # its shape as well as by name (any standalone 32-hex token, any
        # piece of one a cut left behind), so keeping them means not asking
        # at all rather than asking with an empty set.
        return text
    return redact_serials(text, private.serials)


def _mask_address(match: re.Match) -> str:
    value = match.group(0)
    if value in PUBLIC_ADDRESSES:
        return value
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        # Four dotted numbers that are not an address (an octet over 255):
        # not something this rule is about.
        return value
    return ADDRESS


# ---------------------------------------------------------------------------
# The gate: what may not be published
# ---------------------------------------------------------------------------

# Absolute paths a host tool, the operating system, or a package manager
# legitimately names. Everything else absolute is a finding: a bench's run
# directory, build tree, or mount point is more likely than something a
# reader of a release needs, and `--strip-path` names the ones that are not.
SYSTEM_ROOTS = (
    "/Applications", "/Library", "/System", "/bin", "/dev", "/etc", "/lib",
    "/lib32", "/lib64", "/opt", "/proc", "/sbin", "/snap", "/sys", "/usr",
)
# A path as a transcript writes one: after whitespace or a delimiter, never
# after a word character (`12/12 passed`), a colon or another slash (the
# `//` of a URL), or the `>` a placeholder ends with (`<home>/host`).
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:/>~])(/[A-Za-z0-9_.][^\s\"'`)\]},;|]*)"
)
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
# `user@machine` without a domain, which is how an SSH destination and a
# lease holder are written.
_AT_HOST = re.compile(r"(?<![\w.@-])[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9.-]*")
# A whole Ubertooth serial, and a piece of one wide enough to name a board;
# report.py masks both, so one here is a rule that did not run. The piece is
# judged as report.py judges it -- a run that wide carrying both letters and
# digits is a serial and little else -- so a long run of digits alone (a
# counter, a bit string, a field of zeros) is left to read here too.
_HEX_SERIAL = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32}(?![0-9A-Za-z])")
_HEX_PIECE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{24,31}(?![0-9A-Za-z])")
# A serial port named by the serial inside it. report.py keeps the path and
# masks the serial, so a by-id name still carrying digits is a leak.
_BY_ID = re.compile(r"/dev/serial/by-id/\S*\d{4,}\S*")
# A per-user runtime directory, which carries the operator's numeric id.
_RUNTIME_DIR = re.compile(r"/run/user/\d+")
# An SSH invocation. Its destination may be an alias from a private
# configuration file, which has no form of its own to recognize, so the
# command itself is the finding.
_SSH_COMMAND = re.compile(r"(?<![\w-])(?:ssh|scp|sftp|rsync)\s+[-\w@.]")

# How much of a line a finding quotes. Long enough to see what was found and
# short enough that a refusal naming many of them stays readable.
EXCERPT_CHARS = 160

RULE_PATH = "an absolute path this export does not recognize"
RULE_HOME = "a home directory"
RULE_TMP = "a temporary directory"
RULE_RUNTIME = "a per-user runtime directory"
RULE_MAIL = "an electronic mail address"
RULE_AT_HOST = "a user and machine name"
RULE_HOST = "a name in a private domain"
RULE_ADDRESS = "an IP address"
RULE_SERIAL = "a device serial"
RULE_SSH = "a remote-login command"
RULE_NAME = "a private name this export was told to remove"


def _excerpt(line: str, found: str) -> str:
    text = found.strip() or line.strip()
    return text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else "")


def _path_findings(line: str) -> Iterable[str]:
    for match in _ABSOLUTE_PATH.finditer(line):
        path = match.group(1)
        root = "/" + path.split("/", 2)[1] if "/" in path[1:] else path
        if root in SYSTEM_ROOTS or path in SYSTEM_ROOTS:
            continue
        yield path


def line_findings(line: str, private: Private = Private(),
                  *, where: str = "", number: int = 0) -> list[Finding]:
    """Everything in one sanitized line that must not be published.

    Each rule is read over the finished text, so a rule :func:`sanitize` does
    not implement reaches a reader as a refusal rather than as a leak.
    """
    found: list[Finding] = []

    def record(rule: str, text: str) -> None:
        found.append(Finding(rule, where, number, _excerpt(line, text)))

    for name in private.names:
        if _name_pattern(name).search(line) or name in line:
            record(RULE_NAME, name)
    for match in _HOME_PATHS:
        hit = match.search(line)
        if hit:
            record(RULE_HOME, hit.group(0))
    for match in _TMP_PATHS:
        hit = match.search(line)
        if hit:
            record(RULE_TMP, hit.group(0))
    for pattern, rule in (
        (_RUNTIME_DIR, RULE_RUNTIME),
        (_MAIL, RULE_MAIL),
        (_PRIVATE_HOST, RULE_HOST),
        (_SSH_COMMAND, RULE_SSH),
    ):
        hit = pattern.search(line)
        if hit:
            record(rule, hit.group(0))
    if not private.keep_serials:
        for pattern in (_HEX_SERIAL, _BY_ID):
            hit = pattern.search(line)
            if hit:
                record(RULE_SERIAL, hit.group(0))
        for match in _HEX_PIECE.finditer(line):
            piece = match.group(0)
            if any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece):
                record(RULE_SERIAL, piece)
    hit = _AT_HOST.search(line)
    if hit and not _MAIL.search(hit.group(0)):
        record(RULE_AT_HOST, hit.group(0))
    for match in _DOTTED_QUAD.finditer(line):
        if match.group(0) not in PUBLIC_ADDRESSES:
            record(RULE_ADDRESS, match.group(0))
    for path in _path_findings(line):
        record(RULE_PATH, path)
    return found


def findings(text: str, private: Private = Private(),
             *, where: str = "") -> tuple[Finding, ...]:
    """Every finding in ``text``, with the line each was found on."""
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found += line_findings(line, private, where=where, number=number)
    return tuple(found)


# How many findings a refusal names. The rest are counted: a file whose every
# line carries a bench path would otherwise bury the first one.
REPORTED_FINDINGS = 20


def describe(found: Sequence[Finding]) -> str:
    """A refusal naming what was found, bounded in length."""
    shown = [item.describe() for item in found[:REPORTED_FINDINGS]]
    if len(found) > REPORTED_FINDINGS:
        shown.append(f"... and {len(found) - REPORTED_FINDINGS} more")
    return "\n".join(shown)


def require_safe(text: str, private: Private = Private(),
                 *, where: str = "") -> str:
    """``text`` if it may be published; otherwise raise :class:`Unsafe`.

    The export calls this on everything it is about to write, including the
    summary it composed itself, and writes nothing once it raises: a bundle
    that is half sanitized is worse than no bundle.
    """
    found = findings(text, private, where=where)
    if found:
        raise Unsafe(
            f"{where or 'text'} still carries "
            f"{len(found)} thing(s) that must not be published:\n"
            + describe(found)
            + "\n\nSanitize the source, or name the prefix with --strip-path; "
            "the unredacted records stay on the test host."
        )
    return text
