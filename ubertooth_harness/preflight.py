"""Mandatory preflight checks derived from immutable run/resource identity."""

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Mapping, Sequence

from ubertooth_harness.executor import CommandResult
from ubertooth_harness.cases import GUARD_ROLE_PREFIX
from ubertooth_harness import bootloader as bootloader_identity
from ubertooth_harness import libbtbbpin
from ubertooth_harness.manifest import ManifestError, RunManifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness import esp32peer, nrfdk
from ubertooth_harness.patterns import (
    APPLICATION_IMAGE,
    BUILD_STRING_LINE,
    FIRMWARE_VERSION_RE,
    HOST_VERSION_TIMEOUT_S,
    commit_revision,
    compile_info_pattern,
    firmware_version_pattern,
    matched_image,
    revision_pattern,
    same_revision,
)
from ubertooth_harness.outputfiles import output_of
from ubertooth_harness.resources import (
    ESP32_CLASSIC_PEER,
    NRF52840_DK,
    ResourceRegistry,
)
from ubertooth_harness.stateful import StateCaseError
from ubertooth_harness.transitions import (
    API_PATTERN,
    firmware_api,
    format_api,
    host_api_version,
)


class PreflightError(ValueError):
    """Mandatory preflight cannot be planned or validated."""


# The revision a board reports, for the firmware identity checks' reasons.
# `-v` may name the image ahead of the revision, so the revision is the word
# before `(API:`, not the first word after the label.
# "Firmware compile info" reads the same answer out of `-V`. Where the build
# string carried the revision it is read from there; since #352 it is the
# build date alone, so the revision comes off that answer's own
# `Firmware version:` line, which `-V` prints above it.
_COMPILE_INFO_REVISION = re.compile(r"\bubertooth\s+(?P<revision>\S+)\s+\(")
_FIRMWARE_REVISION = {
    "Firmware version": (FIRMWARE_VERSION_RE,),
    # Each is tried in turn, and the first that is there decides. The build
    # string carried the revision until #352 (fork #1230) and is the build
    # date alone since, so a newer board's revision is read off the
    # `Firmware version:` line of the same `-V` answer instead.
    "Firmware compile info": (_COMPILE_INFO_REVISION, FIRMWARE_VERSION_RE),
}
# Fixes for a board whose firmware is not the run's revision, by run kind.
FLASH_FIX = "flash it with this build (the default when building)"
BUILD_AND_FLASH_FIX = (
    "build and flash it with this ref (run-suite --build; Build in the GUI)"
)
NO_FLASH_FIX = (
    "flash it with this build (the default when building: leave out "
    "--no-flash-build, or tick Flash the firmware to the boards first in the GUI)"
)
# The check that reads which bootloader is under the firmware above. Its name
# is what the report and the GUI show, so it is stated once.
BOOTLOADER_CHECK_NAME = "Bootloader identity"


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    name: str
    host: str
    resource_alias: str | None
    resource_identity: Mapping[str, str]
    argv: tuple[str, ...]
    expected_patterns: tuple[str, ...]
    expected_revision: str
    timeout_s: float = 5.0
    # Same question asked a second way, for a fork commit whose tools answer
    # it on a different option, with the pattern that says `argv` answered at
    # all. The fallback runs only when that pattern is missing, so a tool that
    # answered with the wrong version is reported rather than asked twice.
    # Its result then stands in for the check's (sequencer.py).
    fallback_argv: tuple[str, ...] | None = None
    fallback_when_missing: str | None = None
    # Exit statuses this check accepts. The host version check accepts 1 as
    # well: `ubertooth-util -V` prints the host tools' version first and then
    # reads every attached board, exiting 1 if one cannot be read, which is a
    # board this run may not even use. The version line is what the check
    # reads, so the patterns decide it.
    accepted_returncodes: tuple[int, ...] = (0,)
    # Guard-only boards: the firmware API must be the host tools' own.
    # ubertooth-btle may refuse any other, newer as well as older, and stop
    # the board as it goes -- whether it does is its own build and an
    # environment variable the run does not set -- so the guard check will
    # not send such a board its CRC read and the baseline fails; this refuses
    # it before any test instead (transitions._btle_api_refusal).
    required_api: int | None = None
    # How to fix a firmware revision mismatch; FLASH_FIX when empty.
    fix: str = ""
    # The libbtbb the host tools must report, for the host version check.
    # Checked after the patterns above, so its reason can name the revision
    # the tools reported, not only the pattern that did not match.
    libbtbb: "LibbtbbIdentity | None" = None
    # What the run requires of this board's bootloader, for the check that
    # reads `ubertooth-util -B` (bootloader.py). The identities involved are
    # not revisions, so this check writes its own reason rather than going
    # through the firmware wording below.
    bootloader: bootloader_identity.BootloaderExpectation | None = None

    def result_failure(self, result: CommandResult) -> str:
        if result.timed_out:
            return f"{self.name}: command timed out"
        if result.cancelled:
            return f"{self.name}: command canceled"
        if self.bootloader is not None:
            # `-B` exits 1 for a board whose firmware refuses the read and for
            # a version block this host would not read, and the answer itself
            # says which, so the reason comes from the line rather than the
            # exit status.
            output = output_of(result)
            line = bootloader_identity.IDENTITY_LINE_PATTERN
            found = output.search(line)
            output.note_pattern("observed", self.name, line, found)
            for pattern in self.expected_patterns:
                if pattern != line:
                    output.note_pattern(
                        "expected", self.name, pattern, output.search(pattern)
                    )
            installed = bootloader_identity.identity_from_match(found)
            if installed is None and (
                result.returncode not in self.accepted_returncodes
            ):
                # No line and a status this check does not accept: the read
                # failed for a reason of its own (no board, a board another
                # command holds, a libusb error), and naming old firmware
                # would name the wrong fix. The command's own output is in
                # the transcript.
                target = self.resource_alias or self.host
                return (
                    f"{self.name}: {target} did not report its bootloader "
                    f"(ubertooth-util -B exited {result.returncode})"
                )
            failure = self.bootloader.failure(self.resource_alias, installed)
            return f"{self.name}: {failure}" if failure else ""
        if result.returncode not in self.accepted_returncodes:
            return f"{self.name}: command exited {result.returncode}"
        output = output_of(result)
        for pattern in self.expected_patterns:
            present = output.search(pattern)
            output.note_pattern("expected", self.name, pattern, present)
            if present is None:
                target = self.resource_alias or self.host
                match = None
                for expression in _FIRMWARE_REVISION.get(self.name, ()):
                    match = output.search(expression.pattern)
                    if match is not None:
                        break
                # Name both revisions and how to fix it (#135). Only when they
                # really differ: a board on the right commit but another image
                # (btbr, a bench image) also fails this check, and saying it
                # runs the revision the tools are would name no difference at
                # all. That case falls through to the identity mismatch below,
                # which names what was expected.
                if match and not same_revision(self.expected_revision,
                                               match["revision"]):
                    return (
                        f"{self.name}: {target} runs {match['revision']}, the tools "
                        f"are {self.expected_revision}: {self.fix or FLASH_FIX}"
                    )
                # The revision is this run's, so what the check turned down is
                # the image: a board left on `btbr`, or on a bench image, runs
                # none of the tools the other tests call. Say which image it
                # is carrying, because "identity mismatch" against a board
                # reporting the right commit reads like a harness fault.
                image = matched_image(match)
                if image and image != APPLICATION_IMAGE:
                    return (
                        f"{self.name}: {target} runs the {image} image at "
                        f"{match['revision']}, not {APPLICATION_IMAGE}, which "
                        f"the other tools need: {self.fix or FLASH_FIX}"
                    )
                # Name the pattern: the reason line is often all an operator
                # sees, and "mismatch" alone does not say what was expected.
                return (
                    f"{self.name}: identity mismatch for {target}: "
                    f"output does not match {pattern}"
                )
        if self.libbtbb is not None and self.libbtbb.required:
            found = output.search(self.libbtbb.pattern)
            output.note_pattern(
                "expected", f"{self.name} libbtbb", self.libbtbb.pattern, found
            )
            if found is None:
                reported = output.search(self.libbtbb.reported_pattern)
                carries = (
                    f"libbtbb {reported['value']}" if reported
                    else "no libbtbb version at all"
                )
                return (
                    f"{self.name}: the host tools on {self.host} report "
                    f"{carries}, not {self.libbtbb.expected_revision}, which "
                    f"is {self.libbtbb.origin}: {self.libbtbb.fix}"
                )
        if self.required_api is not None:
            reported = output.search(API_PATTERN)
            output.note_pattern("observed", f"{self.name} API", API_PATTERN,
                                reported)
            api = firmware_api(reported[0]) if reported else None
            if api is None or api != self.required_api:
                found = "unreadable" if api is None else format_api(api)
                serial = self.resource_identity.get("serial", "")
                return (
                    f"{self.name}: guard board {self.resource_alias} (serial "
                    f"{serial}) firmware API {found} is not the host tools "
                    f"API {format_api(self.required_api)}; ubertooth-btle may "
                    "stop a board it cannot read the CRC of, so guarded tests "
                    f"cannot run on {self.host}; update its firmware or "
                    "unregister it"
                )
        return ""

    def needs_fallback(self, result: CommandResult) -> bool:
        """Did the command leave the question unanswered, so ask the fallback?"""
        if self.fallback_argv is None or self.fallback_when_missing is None:
            return False
        # A command killed at its deadline, or stopped by the operator, is a
        # failed check: the fallback answering afterwards would hide that the
        # first command hung.
        if result.cancelled or result.timed_out:
            return False
        return output_of(result).search(self.fallback_when_missing) is None


# `-V` is the version command in every tool the fork builds from C, and its
# first line is the host tools' own version, printed before any board is
# looked for (fork #813). Commits before that answered the host version on
# `-H`/`--host-version`, where `-V` was the board's compile info, so the host
# version check asks `-V` first and falls back to `-H`: the fallback output is
# the same line, since both options print it from one function in
# libubertooth.
HOST_VERSION_ARGV = ("ubertooth-util", "-V")
HOST_VERSION_FALLBACK_ARGV = ("ubertooth-util", "-H")

# The label the host version is printed under. Its presence says the option
# was answered; a version under it that is not the resolved one is a failure,
# not a reason to ask the older option.
HOST_VERSION_LABEL = "Host tools version:"

# The start of the line the host version is on: `libubertooth` at the start
# of a line, under the label or, on fork commits before the labels (fork
# 201c3c49), on its own. Anchoring there keeps the firmware lines `-V` prints
# under it out of the match, since one of them names the same revision
# ("Firmware build: ubertooth git-... (builder@host) ...").
_HOST_VERSION_LINE_START = (
    rf"(?m)^(?:{re.escape(HOST_VERSION_LABEL)} )?libubertooth"
)
# The same line as the fork prints it since #352 (fork #1230):
#
#     Host tools version: 2026-11-DEV1+22 (libbtbb 2020-12-R1+79)
#
# One release name for the tools with libbtbb's beside it. The library ABI
# numbers have gone, and so has the parenthesized commit: the release name is
# the version. The label is required here, because nothing else on the line
# marks it: the version is a bare word, and a board's firmware lines under it
# carry versions too.
_RELEASE_LINE_START = rf"(?m)^{re.escape(HOST_VERSION_LABEL)} "
# The libbtbb half of that line, whole: a version is there, and the bracket
# closes at the end of the line. Required even where the check is only about
# the tools' own half, because a line missing it is not the line the fork
# prints and nothing should pass on half an answer.
_LIBBTBB_BRACKET = r"\(libbtbb[ \t]+[^)\n]*[^)\s][ \t]*\)"


def host_version_pattern(expected_revision: str, *, names_release: bool) -> str:
    """The host tools' half of that line, for ``expected_revision``.

    ``names_release`` picks the layout: the release-name line above, or the
    ``libubertooth <abi> (<revision>)`` an older commit prints, with or
    without its label (the labels arrived in fork 201c3c49).
    """
    if names_release:
        # The libbtbb bracket must follow, which is also what ends the
        # version token: without it a longer release name starting with this
        # one would match. The whole bracket, to the end of the line, because
        # the line is exactly this and nothing else: a half-written one, and
        # one with something after the bracket, are not the answer.
        return (
            rf"{_RELEASE_LINE_START}{revision_pattern(expected_revision)}"
            rf"[ \t]+{_LIBBTBB_BRACKET}$"
        )
    # Spaces and tabs, not \s, so a match cannot run past the end of that
    # line onto the version of something else.
    return (
        rf"{_HOST_VERSION_LINE_START}[ \t]+\S+[ \t]+"
        rf"\({revision_pattern(expected_revision)}\)"
    )


def libbtbb_version_pattern(expected_revision: str, *,
                            names_release: bool) -> str:
    """The libbtbb half of that line, for ``expected_revision``.

    Both versions are on the one line, libbtbb's second, and what it carries
    is ``btbb_get_release()``: the release libbtbb's own build names since
    `libbtbb#139`, and before that the commit the installed library was built
    from. ``names_release`` is about the *line*, which the Ubertooth commit
    decides; which of the two ``expected_revision`` is comes from the libbtbb
    side (``libbtbb_identity``).
    """
    if names_release:
        return (
            rf"{_RELEASE_LINE_START}\S+[ \t]+\(libbtbb[ \t]+"
            rf"{revision_pattern(expected_revision)}\)$"
        )
    return (
        rf"{_HOST_VERSION_LINE_START}\b.*\blibbtbb[ \t]+\S+[ \t]+"
        rf"\({revision_pattern(expected_revision)}\)"
    )


def libbtbb_reported_pattern(*, names_release: bool) -> str:
    """The libbtbb half of that line as printed, whatever it carries.

    For a reason that has to name the revision found as well as the one
    wanted.
    """
    if names_release:
        return (
            rf"{_RELEASE_LINE_START}\S+[ \t]+\(libbtbb[ \t]+"
            r"(?P<value>[^)\n]*)\)$"
        )
    return (
        rf"{_HOST_VERSION_LINE_START}\b[^\n]*?\blibbtbb[ \t]+\S+[ \t]+"
        r"\((?P<value>[^)\n]*)\)"
    )


def firmware_identity_patterns(expected_revision: str, *,
                               names_release: bool) -> tuple[str, ...]:
    """What `ubertooth-util -V` must say about the board's firmware.

    The same question the `-v` check asks, asked through the other command,
    so a board that answers one and not the other is caught.

    On a commit whose build stamps a hash, the build string carries the
    revision and is what this reads. Since #352 (fork #1230) that string is
    the build date alone and names no version at all, so the revision comes
    off the `Firmware version:` line of the same answer -- which `-v` reads
    too, but through a different command, over a different pair of control
    requests -- and the build line is required to carry a build string rather
    than the "read failed" or "not reported" libubertooth prints when it has
    none.
    """
    if names_release:
        return (
            firmware_version_pattern(expected_revision),
            BUILD_STRING_LINE,
        )
    return (compile_info_pattern(expected_revision),)


def host_version_patterns(ubertooth, libbtbb=None) -> tuple[str, ...]:
    """Patterns the host tools' version line must match for the resolved sources.

    The line is what `ubertooth-util -V` prints first, and what `-H` printed
    on older fork commits. Its layout, and what the versions on it are, both
    come from the resolved Ubertooth commit (``names_release``). A trailing
    `*` (modified source) does not match either way.

    Preflight passes only the Ubertooth source and carries the libbtbb half
    as a LibbtbbIdentity instead, so its reason can name both revisions and
    where the expectation came from; the build checks its own tools with both
    (build.py).
    """
    names_release = ubertooth.names_release
    patterns = (
        host_version_pattern(
            ubertooth.expected_revision, names_release=names_release
        ),
    )
    if libbtbb is not None:
        # The revision as that build reports it: the release name, or the
        # abbreviation an older build stamps. Not the full commit
        # libbtbb_identity compares against -- this is the build's own check
        # of the library it just built, so git's own abbreviation of it is
        # exactly what the line will carry.
        patterns += (
            libbtbb_version_pattern(
                libbtbb.expected_revision, names_release=names_release
            ),
        )
    return patterns


# How to put a libbtbb other than the wanted one right, by run kind. The
# installed-library fixes name building or installing that commit first: a
# library the run did not resolve is compared against a commit, so a library
# that reports a release string instead (one built from a tarball) cannot be
# made to match by naming any ref at all.
LIBBTBB_INSTALL_FIX = (
    "install libbtbb built from that commit's checkout over the one the tools "
    "load, or run with --build so the run builds it; where the installed "
    "library was built from a different commit on purpose, name that commit "
    "with --libbtbb-source and --libbtbb-ref"
)
LIBBTBB_RESOLVED_FIX = (
    "install libbtbb built from that commit's checkout over the one the tools "
    "load, or name the commit the installed library was built from with "
    "--libbtbb-ref"
)
LIBBTBB_BUILT_FIX = (
    "the run's prefix must come first on PATH and its tools must load the "
    "libbtbb it built; check the build and run it again"
)
LIBBTBB_BUILT_ORIGIN = "the libbtbb this run built"
LIBBTBB_RESOLVED_ORIGIN = "the libbtbb this run resolved (--libbtbb-ref)"
# Why a run that resolved no libbtbb source cannot check one against an
# Ubertooth commit whose version lines name releases. The pin in that commit's
# install.sh is a commit, and what libbtbb reports on the version line may be
# a release name, which is the tag its own checkout is nearest and how far
# past it that commit is: neither is derivable from a commit without that
# checkout to ask. So the run says so rather than comparing the wrong string.
LIBBTBB_RELEASE_NOT_DERIVABLE = (
    "{label} sets LIBBTBB_REF={ref} in install.sh, and a build of that commit "
    "may report the release it came from rather than the commit; working that "
    "out needs the libbtbb checkout, which this run has not got, so the "
    "libbtbb the tools load was not checked. Name it with --libbtbb-source "
    "and --libbtbb-ref (a deployed runner passes --libbtbb-ref fork-pin) to "
    "have it checked"
)


def libbtbb_identity_revision(libbtbb) -> str:
    """What a run must see on the libbtbb half of the version line.

    The release a build of that commit names, where its build names one. Where
    it stamps a commit, the commit in full rather than the abbreviation: a
    short hash would also accept a longer one that merely starts with it, and
    a run comparing a library somebody else built has nothing else to go on.
    """
    if libbtbb.names_release:
        return libbtbb.expected_revision
    return commit_revision(libbtbb.commit)


@dataclass(frozen=True)
class LibbtbbIdentity:
    """Which libbtbb the run's host tools must report, and where that came from.

    The fork's configure step only looks for a symbol, so neither the build
    nor the link stage tells two libbtbb commits apart: the version line is
    the only thing that says which one a tool loads. A run that does not
    check it can measure a different decoder from the one it reports (#294).

    ``expected_revision`` is empty where the run requires no libbtbb at all,
    which is itself worth recording: a reader of such a run must be able to
    see that nothing held its tools to a library, and why.
    """

    expected_revision: str
    origin: str
    fix: str = ""
    # How the version line is laid out, which the Ubertooth commit decides
    # and not libbtbb: the fork rewrote the line in the same change that gave
    # it release names (#352).
    names_release: bool = False

    @property
    def required(self) -> bool:
        return bool(self.expected_revision)

    @property
    def pattern(self) -> str:
        return libbtbb_version_pattern(
            self.expected_revision, names_release=self.names_release
        )

    @property
    def reported_pattern(self) -> str:
        return libbtbb_reported_pattern(names_release=self.names_release)

    def describe(self) -> str:
        """The revision required and where it came from; only for a required
        identity, since one requiring nothing has no revision to name."""
        return f"{self.expected_revision}, {self.origin}"

    def to_details(self) -> dict:
        """What a run record says about this requirement.

        Two different fields, because "this library was required" and "no
        library was required, for this reason" are different facts and a
        reader of the run must be able to tell them apart (#294).
        """
        if not self.required:
            return {"libbtbb_not_checked": self.origin}
        return {"expected_libbtbb": self.describe()}


def libbtbb_identity(ubertooth, libbtbb=None, *, built=False) -> LibbtbbIdentity:
    """Which libbtbb the host tools of this run must be linked against.

    A run that resolved a libbtbb source must load that one, whether it built
    it (``built``) or named the commit its installed library was built from. A
    run that resolved none has no libbtbb of its own, so the expectation is
    the ``LIBBTBB_REF`` the Ubertooth commit under test sets in its own
    ``install.sh``: the only statement of which libbtbb that commit is meant
    to run against.

    What the line must carry for a resolved libbtbb is
    ``libbtbb_identity_revision``: the release a build of that commit names,
    or the commit in full where its build stamps one.

    An identity requiring nothing comes back in two cases, and each records
    its own reason: the Ubertooth commit names no libbtbb at all (fork
    commits from before the pin landed), and it names one by commit while its
    own version lines name releases, which a commit cannot be turned into
    without the libbtbb checkout (LIBBTBB_RELEASE_NOT_DERIVABLE). Nothing is
    claimed in either, so nothing is checked, and the run says so.
    PreflightError is raised where the commit names a libbtbb that cannot be
    compared with what the tools report for a reason somebody can put right,
    rather than let a run measure an unverified library.
    """
    names_release = ubertooth.names_release
    if libbtbb is not None:
        return LibbtbbIdentity(
            libbtbb_identity_revision(libbtbb),
            LIBBTBB_BUILT_ORIGIN if built else LIBBTBB_RESOLVED_ORIGIN,
            LIBBTBB_BUILT_FIX if built else LIBBTBB_RESOLVED_FIX,
            names_release,
        )
    label = f"Ubertooth {ubertooth.short_commit}"

    def refuse(problem: str) -> PreflightError:
        return PreflightError(
            "the libbtbb the run's host tools load cannot be checked: "
            f"{problem}; name the commit the installed library was built "
            "from with --libbtbb-source and --libbtbb-ref"
        )

    try:
        pin = libbtbbpin.fork_pin_at(ubertooth.source_path, ubertooth.commit)
    except libbtbbpin.PinUnreadable as error:
        # A pin the file does name, but as a shell expansion: the problem is
        # the value, not reading the file, and the message must say so.
        raise refuse(f"{label} has a pin this cannot use: {error}") from error
    except (OSError, RuntimeError) as error:
        raise refuse(f"{label} has an install.sh that could not be read: {error}") \
            from error
    if pin is None:
        return LibbtbbIdentity(
            "", f"{label} sets no LIBBTBB_REF in install.sh, so the libbtbb "
            "the tools load was not checked", names_release=names_release,
        )
    if not libbtbbpin.is_commit(pin.ref):
        # The tools report a commit, so a name that can move is nothing to
        # compare with: resolving it here would also make the check mean
        # something different on every run.
        raise refuse(
            f"{label} sets LIBBTBB_REF={pin.ref} in install.sh, which is not "
            "a commit"
        )
    if names_release:
        # A commit, and what the line may carry is a release name. Not a
        # refusal: the run is perfectly able to measure everything else, and
        # a bench that wants this checked says which libbtbb its tools hold.
        return LibbtbbIdentity(
            "",
            LIBBTBB_RELEASE_NOT_DERIVABLE.format(label=label, ref=pin.ref),
            names_release=names_release,
        )
    return LibbtbbIdentity(
        commit_revision(pin.ref),
        f"the LIBBTBB_REF install.sh sets at {label}",
        LIBBTBB_INSTALL_FIX,
        names_release,
    )


_NRFDK = str(Path(nrfdk.__file__).resolve())


def _dk_check(sequence: int, host: str, resource, expected_revision: str) -> PreflightCheck:
    """nrfdk.py identify: pinned nrfutil, the probe attached, VCOM0 present."""
    serial = str(int(resource.identity["jlink serial"]))
    argv = (sys.executable, _NRFDK, "identify", "--jlink", serial)
    if resource.identity.get("serial device"):
        argv += ("--port", resource.identity["serial device"])
    return PreflightCheck(
        check_id=f"preflight-{sequence:03d}-nrf52840-dk-probe",
        name="nRF52840 DK probe",
        host=host,
        resource_alias=resource.alias,
        resource_identity=MappingProxyType(dict(resource.identity)),
        argv=argv,
        expected_patterns=(rf"(?m)^J-Link serial: {serial}$",),
        expected_revision=expected_revision,
        timeout_s=nrfdk.TOOL_TIMEOUT_S * 3,
    )


_ESP32PEER = str(Path(esp32peer.__file__).resolve())


def _peer_check(sequence: int, host: str, resource, expected_revision: str) -> PreflightCheck:
    """esp32peer.py identify: the bridge is there and the board answers.

    Opening the bridge resets the board, so this also leaves it in the
    firmware's boot state before the run.
    """
    serial = resource.identity["bridge serial"]
    device = (resource.identity.get("serial device")
              or esp32peer.default_device(serial))
    argv = (sys.executable, _ESP32PEER, "identify", "--device", device,
            "--address", resource.identity["BD_ADDR"])
    return PreflightCheck(
        check_id=f"preflight-{sequence:03d}-esp32-classic-peer",
        name="ESP32 Classic peer",
        host=host,
        resource_alias=resource.alias,
        resource_identity=MappingProxyType(dict(resource.identity)),
        argv=argv,
        expected_patterns=(
            rf"(?m)^peer address: {re.escape(resource.identity['BD_ADDR'].upper())}$",
            r"(?m)^peer firmware: utesp-\d+\.\d+\.\d+$",
        ),
        expected_revision=expected_revision,
        timeout_s=esp32peer.BOOT_TIMEOUT_S + esp32peer.COMMAND_TIMEOUT_S * 2,
    )


class PreflightPolicy:
    """Create the non-optional minimum checks for selected local resources."""

    def __init__(self, registry: ResourceRegistry, catalog=None):
        from ubertooth_harness.catalog import builtin_catalog

        self.registry = registry
        self.catalog = catalog or builtin_catalog()

    def check_order(self, invocations) -> None:
        """Fail now if a test would run after a firmware write on its device."""
        self._firmware_established_by_suite(invocations)

    def _firmware_established_by_suite(self, invocations) -> set[str]:
        """Ubertooths whose firmware the suite itself writes and verifies.

        Their firmware revision is not required before the run. A test that
        writes firmware must be the last test on its device, so no later test
        runs on firmware the run did not verify.
        """
        uses: dict[str, list[bool]] = {}
        for invocation in invocations:
            establishes = self.catalog.get(invocation.test_name).establishes_firmware
            for role, alias in invocation.resources.items():
                if role.startswith(GUARD_ROLE_PREFIX):
                    continue  # guards are only read; their firmware is not under test
                if self.registry.get(alias).type == "Ubertooth":
                    uses.setdefault(alias, []).append(establishes)
        established = set()
        for alias, flags in uses.items():
            if True in flags and not all(flags[flags.index(True):]):
                raise PreflightError(
                    f"a test after a firmware-writing test would run on {alias} "
                    "with unverified firmware; make the firmware-writing test "
                    f"the last test on {alias}"
                )
            if all(flags):
                established.add(alias)
        return established

    def boards_needing_firmware(self, invocations) -> tuple[str, ...]:
        """Ubertooths whose firmware revision preflight requires, in run order.

        These are the boards a building run flashes with its own firmware
        first (cli.py, the default with --build). Guard-only boards are left
        out: preflight only requires their firmware API to be the host tools'
        own, not the resolved revision. Boards a firmware-writing test
        establishes itself are left out too.
        """
        established = self._firmware_established_by_suite(invocations)
        found: list[str] = []
        for invocation in invocations:
            for role, alias in invocation.resources.items():
                if role.startswith(GUARD_ROLE_PREFIX) or alias in established:
                    continue
                if alias not in found and self.registry.get(alias).type == "Ubertooth":
                    found.append(alias)
        return tuple(found)

    @staticmethod
    def _pattern(text: str) -> str:
        return re.escape(text)

    def compile(
        self,
        invocations: Sequence[TestInvocation],
        manifest: RunManifest,
        *,
        firmware_fix: str = "",
        bootloader: bootloader_identity.BootloaderExpectation | None = None,
    ) -> tuple[PreflightCheck, ...]:
        """The run's preflight checks.

        ``firmware_fix`` says how to fix a board whose firmware is not the
        run's revision (FLASH_FIX by default). ``bootloader`` adds the
        bootloader identity check to every board whose firmware this preflight
        requires; it is left out for a run whose resolved commit cannot report
        a bootloader at all (bootloader.supported_at)."""
        if not invocations:
            raise PreflightError("preflight requires at least one invocation")
        if not manifest.matches_invocations(invocations):
            raise PreflightError("manifest invocations do not match selected tests")
        ubertooth_source = manifest.source("Ubertooth")
        try:
            libbtbb_source = manifest.source("libbtbb")
        except ManifestError:
            libbtbb_source = None
        # The Ubertooth half as a pattern; the libbtbb half as an identity of
        # its own, so a run that builds nothing still has something to hold
        # the installed library to (#294).
        host_patterns = host_version_patterns(ubertooth_source)
        libbtbb = libbtbb_identity(
            ubertooth_source, libbtbb_source, built=bool(manifest.build)
        )

        established = self._firmware_established_by_suite(invocations)
        # Boards bound only as guards must be present and selectable by serial;
        # the test records and compares their firmware but does not require
        # the resolved revision on them.
        tested = {
            alias
            for invocation in invocations
            for role, alias in invocation.resources.items()
            if not role.startswith(GUARD_ROLE_PREFIX)
        }
        host_api = None
        checks = []
        sequence = 0
        seen_hosts = set()
        seen_devices = set()
        for invocation in invocations:
            if invocation.host not in seen_hosts:
                seen_hosts.add(invocation.host)
                sequence += 1
                checks.append(
                    PreflightCheck(
                        check_id=f"preflight-{sequence:03d}-host-version",
                        name="Host version",
                        host=invocation.host,
                        resource_alias=None,
                        resource_identity=MappingProxyType({}),
                        argv=HOST_VERSION_ARGV,
                        expected_patterns=host_patterns,
                        expected_revision=ubertooth_source.expected_revision,
                        timeout_s=HOST_VERSION_TIMEOUT_S,
                        fallback_argv=HOST_VERSION_FALLBACK_ARGV,
                        fallback_when_missing=re.escape(HOST_VERSION_LABEL),
                        accepted_returncodes=(0, 1),
                        libbtbb=libbtbb,
                    )
                )
            for alias in invocation.resources.values():
                resource = self.registry.get(alias)
                if resource.type == NRF52840_DK and alias not in seen_devices:
                    # Its probe and VCOM0 port; the test itself flashes and
                    # verifies the role firmware (advertiser_cases.py).
                    seen_devices.add(alias)
                    sequence += 1
                    checks.append(_dk_check(sequence, invocation.host, resource,
                                            ubertooth_source.expected_revision))
                    continue
                if (resource.type == ESP32_CLASSIC_PEER
                        and alias not in seen_devices):
                    # Its bridge and the firmware behind it; the test itself
                    # brings the pair up (classic_cases.py).
                    seen_devices.add(alias)
                    sequence += 1
                    checks.append(_peer_check(sequence, invocation.host, resource,
                                              ubertooth_source.expected_revision))
                    continue
                if resource.type != "Ubertooth" or alias in seen_devices:
                    continue
                seen_devices.add(alias)
                serial = resource.identity.get("serial")
                if not serial:
                    raise PreflightError(
                        f"Ubertooth {alias} requires a registered serial identity"
                    )
                selected_serial = serial.lower()
                expected = ubertooth_source.expected_revision
                common = ("ubertooth-util", "-U", selected_serial)
                device_checks = (
                    (
                        "Firmware version",
                        common + ("-v",),
                        (firmware_version_pattern(expected),),
                    ),
                    (
                        "Firmware compile info",
                        common + ("-V",),
                        firmware_identity_patterns(
                            expected, names_release=ubertooth_source.names_release
                        ),
                    ),
                    (
                        "Device serial",
                        common + ("-s",),
                        (rf"Serial No:\s+{self._pattern(serial.lower())}(?:\s|$)",),
                    ),
                )
                required_api = None
                if alias in established or alias not in tested:
                    device_checks = tuple(
                        check for check in device_checks if check[0] == "Device serial"
                    )
                if alias not in tested:
                    # Guard-only: refuse the run before any test if its API
                    # is not the host tools' own, either way about.
                    if host_api is None:
                        try:
                            host_api = host_api_version(ubertooth_source)
                        except StateCaseError as error:
                            raise PreflightError(str(error)) from error
                    required_api = host_api
                    device_checks += (
                        ("Guard firmware API", common + ("-v",), (r"Firmware version:",)),
                    )
                if (
                    bootloader is not None
                    and alias in tested
                    and alias not in established
                ):
                    # Which bootloader is underneath the firmware this run
                    # just required. Not for a guard-only board, whose
                    # firmware may be older than the read, nor for a board a
                    # firmware-writing test establishes itself: both can be
                    # carrying firmware that refuses the request.
                    device_checks += (
                        (
                            BOOTLOADER_CHECK_NAME,
                            bootloader_identity.identity_argv(selected_serial),
                            bootloader.patterns(),
                        ),
                    )
                for name, argv, patterns in device_checks:
                    sequence += 1
                    checks.append(
                        PreflightCheck(
                            check_id=(
                                f"preflight-{sequence:03d}-"
                                f"{name.lower().replace(' ', '-')}"
                            ),
                            name=name,
                            host=invocation.host,
                            resource_alias=alias,
                            resource_identity=MappingProxyType(
                                dict(resource.identity)
                            ),
                            argv=argv,
                            expected_patterns=patterns,
                            expected_revision=expected,
                            timeout_s=(
                                bootloader_identity.IDENTITY_TIMEOUT_S
                                if name == BOOTLOADER_CHECK_NAME else 5.0
                            ),
                            required_api=(
                                required_api if name == "Guard firmware API" else None
                            ),
                            fix=firmware_fix,
                            bootloader=(
                                bootloader if name == BOOTLOADER_CHECK_NAME else None
                            ),
                        )
                    )
        return tuple(checks)
