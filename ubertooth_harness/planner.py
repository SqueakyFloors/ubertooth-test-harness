"""Compile validated named tests into deterministic resource-bound invocations."""

from dataclasses import dataclass, field
import itertools
import re
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness.cases import GUARD_ROLE_PREFIX
from ubertooth_harness.catalog import CatalogError, TestCatalog
from ubertooth_harness.hostneeds import (
    NotApplicable,
    platform_capabilities,
    split_cases,
    unmet,
)
from ubertooth_harness.plan import Plan
from ubertooth_harness.resources import RegistryError, ResourceRegistry
from ubertooth_harness.topology import PowerIsolationAction, plan_exclusive_ubertooth


class PlanningError(ValueError):
    """A valid plan cannot be satisfied by registered resources."""


# Role names for the further boards an "alternate Ubertooths" flash test
# writes in turn; the plan-selected board keeps the "Ubertooth" role.
ALTERNATED_ROLE_PREFIX = "alternated Ubertooth "
_FULL_SERIAL = re.compile(r"[0-9a-f]{32}")


def alternated_roles(resources: Mapping[str, str]) -> tuple[str, ...]:
    """Alternated-board roles of an invocation, in flash order."""
    return tuple(
        sorted(
            (role for role in resources if role.startswith(ALTERNATED_ROLE_PREFIX)),
            key=lambda role: int(role.removeprefix(ALTERNATED_ROLE_PREFIX)),
        )
    )


@dataclass(frozen=True)
class TestInvocation:
    invocation_id: str
    test_name: str
    host: str
    arguments: Mapping
    resources: Mapping[str, str]
    resource_identities: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    power_isolation: tuple[PowerIsolationAction, ...] = ()
    # `exclusive Ubertooth: true`; with no power actions (no other registered
    # board on the host) the run still checks, read-only, that the selected
    # board is the only Ubertooth enumerated.
    exclusive_ubertooth: bool = False


@dataclass(frozen=True)
class CompiledSuite:
    invocations: tuple[TestInvocation, ...]
    # Tests a selected host cannot run (test-wide needs); one per host.
    not_applicable: tuple[NotApplicable, ...] = ()

    def for_host(self, host: str) -> tuple[NotApplicable, ...]:
        return tuple(item for item in self.not_applicable if item.host == host)


class PlanCompiler:
    def __init__(self, catalog: TestCatalog, registry: ResourceRegistry):
        self.catalog = catalog
        self.registry = registry

    def _alternated(self, step, host, arguments, resources, identities):
        """Bind every other registered Ubertooth on the host as an alternated board.

        The flash test writes them in turn after the selected board, so they
        are invocation resources: preflight, the firmware-order check, the
        manifest, and the report all see them.
        """
        name = step.test_name
        selected = resources["Ubertooth"]
        others = tuple(
            alias
            for alias in self.registry.resolve_hardware(host, "Ubertooth", "each")
            if alias != selected
        )
        if not others:
            raise PlanningError(
                f"{name}: alternate Ubertooths needs at least two registered "
                f"Ubertooths on {host}"
            )
        boards = (selected, *others)
        for alias in boards:
            serial = self.registry.get(alias).identity.get("serial", "")
            # libubertooth matches -U against the END of any attached board's
            # serial, so a short selector could reach an unregistered board.
            if not isinstance(serial, str) or not _FULL_SERIAL.fullmatch(serial.lower()):
                raise PlanningError(
                    f"{name}: {alias} needs a full 32-hex-digit serial to be alternated"
                )
        flashes = arguments.get("flashes")
        if isinstance(flashes, int) and flashes % len(boards):
            raise PlanningError(
                f"{name}: {flashes} flashes cannot alternate evenly across "
                f"{len(boards)} Ubertooths on {host}; use a multiple of {len(boards)}"
            )
        resources = dict(resources)
        identities = dict(identities)
        for index, alias in enumerate(others, 2):
            role = f"{ALTERNATED_ROLE_PREFIX}{index}"
            resources[role] = alias
            identities[role] = MappingProxyType(dict(self.registry.get(alias).identity))
        return MappingProxyType(resources), MappingProxyType(identities)

    def _guarded(self, host, resources, identities):
        """Bind every other registered Ubertooth on the host as a guard board.

        Guards are invocation resources, so preflight, the manifest, and the
        report see them; the sequencer reads them before and after the test.
        A host with one registered Ubertooth has no guards.
        """
        bound = set(resources.values())
        others = tuple(
            alias
            for alias in self.registry.resolve_hardware(host, "Ubertooth", "each")
            if alias not in bound
        )
        if not others:
            return resources, identities
        # Full-serial selectors are checked for the host that runs (CLI and
        # sequencer), so a placeholder on another host does not stop a run.
        resources = dict(resources)
        identities = dict(identities)
        for index, alias in enumerate(others, 1):
            role = f"{GUARD_ROLE_PREFIX}{index}"
            resources[role] = alias
            identities[role] = MappingProxyType(dict(self.registry.get(alias).identity))
        return MappingProxyType(resources), MappingProxyType(identities)

    def compile_suite(self, plan: Plan, suite: str) -> "CompiledSuite":
        """Invocations, and the tests each host was left out of (not applicable)."""
        invocations = []
        not_applicable: list[NotApplicable] = []
        sequence = 0
        try:
            for step in plan.steps(suite):
                step_start = len(invocations)
                definition = self.catalog.get(step.test_name)
                arguments = step.arguments
                host_selector = arguments.get(
                    "host", arguments.get("hosts", "each")
                )
                hosts = self.registry.resolve_hosts(host_selector)
                # Test-wide needs (the plan's platforms: and the definition's)
                # leave a host out as not applicable; a test no named host can
                # run is a plan error.
                eligible_hosts = []
                left_out = []
                for alias in hosts:
                    capabilities = platform_capabilities(
                        alias, self.registry.get(alias).platform
                    )
                    reason = next(
                        (
                            unmet(needs, capabilities)
                            for needs in definition.test_needs(arguments)
                            if unmet(needs, capabilities)
                        ),
                        "",
                    )
                    if reason:
                        left_out.append(NotApplicable(alias, step.test_name, reason))
                    else:
                        eligible_hosts.append(alias)
                if not eligible_hosts:
                    raise PlanningError(
                        f"{step.test_name}: no selected host can run it: "
                        + "; ".join(f"{item.host}: {item.reason}" for item in left_out)
                    )
                not_applicable.extend(left_out)
                for host in eligible_hosts:
                    role_names = tuple(definition.hardware_roles or {})
                    if len(role_names) > 1 and any(
                        arguments.get(role) == "each" for role in role_names
                    ):
                        raise PlanningError(
                            f"{step.test_name}: multi-role tests require explicit "
                            "role aliases; all-pairs selection is not implemented"
                        )
                    choices = []
                    for role in role_names:
                        resource_type = definition.hardware_roles[role]
                        selector = arguments.get(role, "default")
                        choices.append(
                            self.registry.resolve_hardware(
                                host, resource_type, selector
                            )
                        )
                    combinations = itertools.product(*choices) if choices else [()]
                    for selected in combinations:
                        if len(set(selected)) != len(selected):
                            continue
                        sequence += 1
                        resources = MappingProxyType(dict(zip(role_names, selected)))
                        identities = MappingProxyType({
                            role: MappingProxyType(
                                dict(self.registry.get(alias).identity)
                            )
                            for role, alias in resources.items()
                        })
                        if arguments.get("alternate Ubertooths") is True:
                            resources, identities = self._alternated(
                                step, host, arguments, resources, identities
                            )
                        if definition.guards_other_ubertooths:
                            resources, identities = self._guarded(
                                host, resources, identities
                            )
                        power_isolation = ()
                        exclusive = arguments.get("exclusive Ubertooth") is True
                        if exclusive:
                            selected_ubertooth = resources.get("Ubertooth")
                            if selected_ubertooth is None:
                                raise PlanningError(
                                    f"{step.test_name}: exclusive topology requires "
                                    "an Ubertooth role"
                                )
                            power_isolation = plan_exclusive_ubertooth(
                                self.registry, host, selected_ubertooth
                            )
                        invocations.append(
                            TestInvocation(
                                invocation_id=f"{sequence:03d}-{step.test_name}",
                                test_name=step.test_name,
                                host=host,
                                arguments=arguments,
                                resources=resources,
                                resource_identities=identities,
                                power_isolation=power_isolation,
                                exclusive_ubertooth=exclusive,
                            )
                        )
                if len(invocations) == step_start:
                    raise PlanningError(
                        f"{step.test_name}: no valid distinct resource assignment"
                    )
                self._check_some_case_applies(
                    definition, invocations[step_start:], left_out
                )
        except RegistryError as error:
            raise PlanningError(str(error)) from error
        if not invocations:
            raise PlanningError(f"suite '{suite}' produced no runnable invocations")
        return CompiledSuite(tuple(invocations), tuple(not_applicable))

    def compile(self, plan: Plan, suite: str) -> tuple[TestInvocation, ...]:
        return self.compile_suite(plan, suite).invocations

    def _check_some_case_applies(self, definition, invocations, left_out):
        """A plan error when, by platform alone, no case of the test can run
        on any of its hosts (for example only BlueZ cases on macOS hosts)."""
        reasons = [f"{item.host}: {item.reason}" for item in left_out]
        for invocation in invocations:
            try:
                cases = definition.command_cases(
                    invocation.arguments, invocation.resources,
                    invocation.resource_identities,
                )
            except CatalogError:
                return  # the run reports a builder error; nothing to decide
            capabilities = platform_capabilities(
                invocation.host, self.registry.get(invocation.host).platform
            )
            runnable, omitted = split_cases(cases, capabilities)
            if runnable:
                return
            reasons += sorted({f"{invocation.host}: {reason}" for _case, reason in omitted})
        if invocations:
            raise PlanningError(
                f"{definition.name}: no case can run on any selected host: "
                + "; ".join(reasons)
            )
