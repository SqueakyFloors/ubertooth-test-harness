"""Compile validated named tests into deterministic resource-bound invocations."""

from dataclasses import dataclass, field
import itertools
import re
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness.cases import GUARD_ROLE_PREFIX
from ubertooth_harness.catalog import TestCatalog
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

    def compile(self, plan: Plan, suite: str) -> tuple[TestInvocation, ...]:
        invocations = []
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
                requested_platforms = arguments.get("platforms")
                if requested_platforms is not None:
                    mismatched_hosts = [
                        alias
                        for alias in hosts
                        if self.registry.get(alias).platform not in requested_platforms
                    ]
                    if mismatched_hosts:
                        raise PlanningError(
                            f"{step.test_name}: selected host platform is unsupported: "
                            f"{', '.join(mismatched_hosts)}"
                        )
                eligible_hosts = list(hosts)
                if not eligible_hosts:
                    raise PlanningError(
                        f"{step.test_name}: no selected host matches platform requirement"
                    )
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
        except RegistryError as error:
            raise PlanningError(str(error)) from error
        if not invocations:
            raise PlanningError(f"suite '{suite}' produced no runnable invocations")
        return tuple(invocations)
