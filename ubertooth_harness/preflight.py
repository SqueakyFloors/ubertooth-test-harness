"""Mandatory preflight checks derived from immutable run/resource identity."""

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Mapping, Sequence

from ubertooth_harness.executor import CommandResult
from ubertooth_harness.cases import GUARD_ROLE_PREFIX
from ubertooth_harness.manifest import ManifestError, RunManifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness import esp32peer, nrfdk
from ubertooth_harness.patterns import (
    FIRMWARE_VERSION_RE,
    HOST_VERSION_TIMEOUT_S,
    compile_info_pattern,
    firmware_version_pattern,
    revision_pattern,
)
from ubertooth_harness.resources import (
    ESP32_CLASSIC_PEER,
    NRF52840_DK,
    ResourceRegistry,
)
from ubertooth_harness.stateful import StateCaseError
from ubertooth_harness.transitions import firmware_api, format_api, host_api_version


class PreflightError(ValueError):
    """Mandatory preflight cannot be planned or validated."""


# The revision a board reports, for the firmware identity checks' reasons.
# `-v` may name the image ahead of the revision, so the revision is the word
# before `(API:`, not the first word after the label.
_FIRMWARE_REVISION = {
    "Firmware version": FIRMWARE_VERSION_RE,
    "Firmware compile info": re.compile(r"\bubertooth\s+(\S+)\s+\("),
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
    # Guard-only boards: the firmware API must be no older than the host
    # tools' (ubertooth-btle would stop older firmware). Newer is fine.
    minimum_api: int | None = None
    # How to fix a firmware revision mismatch; FLASH_FIX when empty.
    fix: str = ""

    def result_failure(self, result: CommandResult) -> str:
        if result.timed_out:
            return f"{self.name}: command timed out"
        if result.cancelled:
            return f"{self.name}: command canceled"
        if result.returncode not in self.accepted_returncodes:
            return f"{self.name}: command exited {result.returncode}"
        output = result.stdout + result.stderr
        for pattern in self.expected_patterns:
            if not re.search(pattern, output):
                target = self.resource_alias or self.host
                found = _FIRMWARE_REVISION.get(self.name)
                match = found.search(output) if found else None
                if match:
                    # Name both revisions and how to fix it (#135).
                    return (
                        f"{self.name}: {target} runs {match.group(1)}, the tools "
                        f"are {self.expected_revision}: {self.fix or FLASH_FIX}"
                    )
                # Name the pattern: the reason line is often all an operator
                # sees, and "mismatch" alone does not say what was expected.
                return (
                    f"{self.name}: identity mismatch for {target}: "
                    f"output does not match {pattern}"
                )
        if self.minimum_api is not None:
            api = firmware_api(output)
            if api is None or api < self.minimum_api:
                found = "unreadable" if api is None else format_api(api)
                serial = self.resource_identity.get("serial", "")
                return (
                    f"{self.name}: guard board {self.resource_alias} (serial "
                    f"{serial}) firmware API {found} is older than the host "
                    f"tools API {format_api(self.minimum_api)}; ubertooth-btle "
                    "would stop it, so guarded tests cannot run on "
                    f"{self.host}; update its firmware or unregister it"
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
        output = result.stdout + result.stderr
        return re.search(self.fallback_when_missing, output) is None


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


def host_version_patterns(ubertooth, libbtbb=None) -> tuple[str, ...]:
    """Patterns the host tools' version line must match for the resolved sources.

    The line is what `ubertooth-util -V` prints first, and what `-H` printed
    on older fork commits, with or without its label. A trailing `*`
    (modified source) does not match.
    """
    # Spaces and tabs, not \s, so a match cannot run past the end of that
    # line onto the version of something else.
    patterns = (
        rf"{_HOST_VERSION_LINE_START}[ \t]+\S+[ \t]+"
        rf"\({revision_pattern(ubertooth.expected_revision)}\)",
    )
    if libbtbb is not None:
        # Both versions are on that one line, libbtbb's second.
        patterns += (
            rf"{_HOST_VERSION_LINE_START}\b.*\blibbtbb[ \t]+\S+[ \t]+"
            rf"\({revision_pattern(libbtbb.expected_revision)}\)",
        )
    return patterns


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
        out: preflight only requires their firmware API to be no older than
        the host tools', not the resolved revision. Boards a firmware-writing
        test establishes itself are left out too.
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
    ) -> tuple[PreflightCheck, ...]:
        """The run's preflight checks; ``firmware_fix`` says how to fix a
        board whose firmware is not the run's revision (FLASH_FIX by default)."""
        if not invocations:
            raise PreflightError("preflight requires at least one invocation")
        if not manifest.matches_invocations(invocations):
            raise PreflightError("manifest invocations do not match selected tests")
        ubertooth_source = manifest.source("Ubertooth")
        try:
            libbtbb_source = manifest.source("libbtbb")
        except ManifestError:
            libbtbb_source = None
        host_patterns = host_version_patterns(ubertooth_source, libbtbb_source)

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
                        (compile_info_pattern(expected),),
                    ),
                    (
                        "Device serial",
                        common + ("-s",),
                        (rf"Serial No:\s+{self._pattern(serial.lower())}(?:\s|$)",),
                    ),
                )
                minimum_api = None
                if alias in established or alias not in tested:
                    device_checks = tuple(
                        check for check in device_checks if check[0] == "Device serial"
                    )
                if alias not in tested:
                    # Guard-only: refuse the run before any test if its API
                    # is older than the host tools'.
                    if host_api is None:
                        try:
                            host_api = host_api_version(ubertooth_source)
                        except StateCaseError as error:
                            raise PreflightError(str(error)) from error
                    minimum_api = host_api
                    device_checks += (
                        ("Guard firmware API", common + ("-v",), (r"Firmware version:",)),
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
                            minimum_api=(
                                minimum_api if name == "Guard firmware API" else None
                            ),
                            fix=firmware_fix,
                        )
                    )
        return tuple(checks)
