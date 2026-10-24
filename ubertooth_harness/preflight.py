"""Mandatory preflight checks derived from immutable run/resource identity."""

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from ubertooth_harness.executor import CommandResult
from ubertooth_harness.cases import GUARD_ROLE_PREFIX
from ubertooth_harness.manifest import ManifestError, RunManifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.resources import ResourceRegistry
from ubertooth_harness.stateful import StateCaseError
from ubertooth_harness.transitions import firmware_api, format_api, host_api_version


class PreflightError(ValueError):
    """Mandatory preflight cannot be planned or validated."""


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
    # Guard-only boards: the firmware API must be no older than the host
    # tools' (ubertooth-btle would stop older firmware). Newer is fine.
    minimum_api: int | None = None

    def result_failure(self, result: CommandResult) -> str:
        if result.timed_out:
            return f"{self.name}: command timed out"
        if result.cancelled:
            return f"{self.name}: command canceled"
        if result.returncode != 0:
            return f"{self.name}: command exited {result.returncode}"
        output = result.stdout + result.stderr
        for pattern in self.expected_patterns:
            if not re.search(pattern, output):
                target = self.resource_alias or self.host
                return f"{self.name}: identity mismatch for {target}"
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


def host_version_patterns(ubertooth, libbtbb=None) -> tuple[str, ...]:
    """Patterns `ubertooth-util -H` output must match for the resolved sources.

    A trailing `*` (modified source) does not match.
    """
    patterns = (
        rf"libubertooth\s+\S+\s+\({re.escape(ubertooth.expected_revision)}\)",
    )
    if libbtbb is not None:
        patterns += (
            rf"libbtbb\s+\S+\s+\({re.escape(libbtbb.expected_revision)}\)",
        )
    return patterns


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

    @staticmethod
    def _pattern(text: str) -> str:
        return re.escape(text)

    def compile(
        self,
        invocations: Sequence[TestInvocation],
        manifest: RunManifest,
    ) -> tuple[PreflightCheck, ...]:
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
        seen_ubertooths = set()
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
                        argv=("ubertooth-util", "-H"),
                        expected_patterns=host_patterns,
                        expected_revision=ubertooth_source.expected_revision,
                    )
                )
            for alias in invocation.resources.values():
                resource = self.registry.get(alias)
                if resource.type != "Ubertooth" or alias in seen_ubertooths:
                    continue
                seen_ubertooths.add(alias)
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
                        (rf"Firmware version:\s+{self._pattern(expected)}(?:\s|\()",),
                    ),
                    (
                        "Firmware compile info",
                        common + ("-V",),
                        (rf"\bubertooth\s+{self._pattern(expected)}\s+\(",),
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
                        )
                    )
        return tuple(checks)
