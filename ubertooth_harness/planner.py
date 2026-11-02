"""Compile validated named tests into deterministic resource-bound invocations."""

from dataclasses import dataclass, field
import itertools
import re
from types import MappingProxyType
from typing import Mapping

from ubertooth_harness.cases import ALTERNATED_ROLE_PREFIX, GUARD_ROLE_PREFIX
from ubertooth_harness.catalog import CatalogError, TestCatalog
from ubertooth_harness.hostneeds import (
    Blocked,
    NotApplicable,
    platform_capabilities,
    split_cases,
    unmet,
)
from ubertooth_harness.plan import Plan
from ubertooth_harness.resources import (
    MissingRequiredHardware,
    MissingResource,
    RegistryError,
    ResourceRegistry,
)
from ubertooth_harness.rf import (
    PlacementRequirement,
    describe as describe_placement,
)
from ubertooth_harness.topology import PowerIsolationAction, plan_exclusive_ubertooth


# Any placement at all: asks ``registry.placed`` whether a host has a unit
# that could take a role wherever it sat, which tells absence from a
# placement a unit cannot satisfy. It is falsy, as a requirement that asks
# for nothing, so it is for ``placed`` only: as ``resolve_hardware``'s
# ``placement`` it would read as no requirement given.
ANY_PLACEMENT = PlacementRequirement()


class PlanningError(ValueError):
    """A valid plan cannot be satisfied by registered resources."""


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
    # The RF placement of every bound resource that has one (rf.py), by role,
    # so the manifest, run.jsonl, and the report say what the run's numbers
    # were taken at. A role whose resource has no `rf:` is absent.
    resource_placements: Mapping[str, Mapping] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # What each role's placement had to satisfy, by role, whether the plan
    # asked for it or the test's own default did. Recorded because it is why
    # a role got the resource it did: a host default passed over, or a board
    # `each` left out, is otherwise invisible in the run record.
    placement_requirements: Mapping[str, Mapping] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class CompiledSuite:
    invocations: tuple[TestInvocation, ...]
    # Tests left out: what a selected host cannot run (test-wide needs, RF
    # placement), and what this registry has not got the hardware for.
    not_applicable: tuple[NotApplicable, ...] = ()
    # Host aliases a step named that this registry has not got. No run is
    # ever made for them, so what they left out belongs to the record of
    # every run of the suite rather than to no record at all.
    absent_hosts: tuple[str, ...] = ()
    # Required tests this bench cannot run because the hardware they are
    # about is not registered (hostneeds.Blocked). Not applicable would read
    # as a pass, so they are recorded and the run that would have run them
    # fails; every other step still compiles.
    blocked: tuple[Blocked, ...] = ()

    def blocked_for_host(self, host: str) -> tuple[Blocked, ...]:
        """What this host's run cannot run and must not pass without.

        Each record is for a host the registry has, so it belongs to that
        host's run alone: another bench's missing hub is not this run's
        failure.
        """
        return tuple(item for item in self.blocked if item.host == host)

    def for_host(self, host: str) -> tuple[NotApplicable, ...]:
        """What this host's run records: what this host was left out of, and
        what a host the registry has not got was left out of, since no run of
        its own will ever carry that. Each item names its own host.

        One record per test, and nothing about a test this host runs: the
        live run view matches a planning record to the test by name, so a
        second record would mark the test's own row not applicable, or show
        the test twice. A step this host has its own reason for keeps that
        reason, which says more to the host running it. So a second step
        naming the same test for an absent host is not recorded here.
        """
        mine = tuple(item for item in self.not_applicable if item.host == host)
        covered = {item.test_name for item in mine}
        covered |= {
            item.test_name for item in self.invocations if item.host == host
        }
        absent = []
        for item in self.not_applicable:
            if item.host not in self.absent_hosts or item.test_name in covered:
                continue
            # Two steps may name the same test for two hosts the registry has
            # not got; this run says it once.
            covered.add(item.test_name)
            absent.append(item)
        return mine + tuple(absent)


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
            # The second board is what the test is about, so this bench has
            # not got the hardware for it; compile_suite records the step as
            # blocked rather than ending the compile.
            raise MissingRequiredHardware(
                f"alternate Ubertooths needs at least two registered "
                f"Ubertooths on {host}, which has one"
            )
        boards = (selected, *others)
        for alias in boards:
            serial = self.registry.get(alias).identity.get("serial", "")
            # libubertooth matches -U against the start (a prefix) of any
            # attached board's serial, so a short selector could reach an
            # unregistered board.
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

    def _placements(self, resources: Mapping[str, str]) -> Mapping[str, Mapping]:
        """The recorded RF placement of every bound resource that has one."""
        return MappingProxyType({
            role: MappingProxyType(self.registry.get(alias).rf.to_dict())
            for role, alias in resources.items()
            if self.registry.get(alias).rf is not None
        })

    def _placement_missing(self, definition, host, arguments, wanted) -> str:
        """Why the host cannot place a role as the test needs, or ''.

        Only for a role the plan left to the harness (``default`` or
        ``each``): an explicit alias that cannot satisfy the requirement is a
        mistake in the plan, and ``resolve_hardware`` refuses it.

        A host that could not take the role wherever its units sat is not
        answered here: that is absence, which binding the role reports in its
        own words, naming the role rather than a placement.
        """
        for role, placement in wanted.items():
            selector = arguments.get(role, "default")
            if selector not in ("default", "each"):
                continue
            resource_type = definition.hardware_roles[role]
            if self.registry.placed(host, resource_type, placement, role):
                continue
            # Only the units that could take the role at all: a host where
            # none can has not got the hardware, which binding reports in its
            # own words, and the rest are nothing to do with the placement.
            candidates = self.registry.placed(
                host, resource_type, ANY_PLACEMENT, role
            )
            if not candidates:
                continue
            have = "; ".join(
                f"{alias} has {describe_placement(self.registry.get(alias).rf)}"
                for alias in candidates
            )
            return (
                f"no {resource_type} on this host has {placement.describe()} "
                f"for the {role} ({have})"
            )
        return ""

    def _selected_hosts(self, selector, strict: bool):
        """(hosts to consider, aliases this registry has not got).

        ``each`` covers what the registry has, so nothing can be absent. An
        explicit list names a bench, and this may not be that bench: each
        alias is resolved on its own, so the hosts that are here are kept and
        the others are named. In strict mode every absent alias is named in
        one refusal, rather than one per run of the command.
        """
        if not isinstance(selector, (tuple, list, str)) or selector == "each":
            return self.registry.resolve_hosts(selector), ()
        aliases = (selector,) if isinstance(selector, str) else tuple(selector)
        if not aliases:
            # The registry's own refusal for an empty selector.
            return self.registry.resolve_hosts(selector), ()
        kept: list[str] = []
        absent: list[str] = []
        for alias in aliases:
            try:
                kept.extend(self.registry.resolve_hosts([alias]))
            except MissingResource:
                absent.append(alias)
        if absent and strict:
            raise MissingResource(
                "this registry has no host "
                + ", ".join(f"'{alias}'" for alias in absent)
            )
        return tuple(kept), tuple(absent)

    def _role_choices(self, definition, host, arguments, wanted):
        """The resources each hardware role may be bound to, per role."""
        choices = []
        for role in definition.hardware_roles or {}:
            resource_type = definition.hardware_roles[role]
            selector = arguments.get(role, "default")
            choices.append(
                self.registry.resolve_hardware(
                    host, resource_type, selector, role, wanted.get(role),
                )
            )
        return choices

    def compile_suite(
        self, plan: Plan, suite: str, *, strict_hardware: bool = False
    ) -> "CompiledSuite":
        """Invocations, and the tests each host was left out of (not applicable).

        One plan is compiled against every bench's registry, so a step may
        name a host this registry has not got, or a device no host here has.
        That is recorded as not applicable and left out, the way a platform
        mismatch is, rather than ending the compile: otherwise one step for
        one bench's hardware stops every other bench running the plan at all.

        Hardware a step is *about*, rather than hardware it merely binds, is
        recorded as blocked instead (``CompiledSuite.blocked``): the step is
        still left out of the invocations, so every unrelated step compiles
        and runs, but the run it belonged to fails. That is what tells a
        plan written for another bench from a gate this bench meant to
        measure and cannot.

        ``strict_hardware`` makes either absence an error, which is what
        ``validate-plan --hardware`` wants: there a registry is named on
        purpose, so a host or a role it cannot provide is either a typo in
        the plan or a bench that is not set up as the plan expects.
        """
        invocations = []
        not_applicable: list[NotApplicable] = []
        absent_hosts: set[str] = set()
        blocked_tests: list[Blocked] = []
        sequence = 0
        try:
            for step in plan.steps(suite):
                step_start = len(invocations)
                definition = self.catalog.get(step.test_name)
                arguments = step.arguments
                role_names = tuple(definition.hardware_roles or {})
                if len(role_names) > 1 and any(
                    arguments.get(role) == "each" for role in role_names
                ):
                    raise PlanningError(
                        f"{step.test_name}: a multi-role test cannot select a "
                        "role with 'each'; name each role by alias, or narrow "
                        "it with rf:; all-pairs selection is not implemented"
                    )
                wanted = definition.placement_requirements(arguments)
                # What each role could take on each host, for the refusal
                # when no two roles can have different devices.
                host_choices: dict[str, dict[str, tuple[str, ...]]] = {}
                host_selector = arguments.get(
                    "host", arguments.get("hosts", "each")
                )
                hosts, absent = self._selected_hosts(host_selector, strict_hardware)
                absent_hosts.update(absent)
                # The reason names the host itself: the live run view shows a
                # planning record by its reason alone, and "this registry has
                # no such host" there would read as being about the host the
                # run is on.
                left_out = [
                    NotApplicable(
                        alias, step.test_name,
                        f"{alias} is not a host in this registry",
                    )
                    for alias in absent
                ]
                # First what the hosts that are here can run at all: the
                # plan's platforms: and the definition's needs, then the RF
                # placement each role must have. A host is left out as not
                # applicable, and a step no host here can run this way is a
                # plan error, as it was before hardware absence was recorded.
                candidates = []
                blocked: list[NotApplicable] = []
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
                    # A host with no board placed as the test needs is left
                    # out the same way, naming what was missing.
                    reason = reason or self._placement_missing(
                        definition, alias, arguments, wanted
                    )
                    if reason:
                        blocked.append(NotApplicable(alias, step.test_name, reason))
                    else:
                        candidates.append(alias)
                left_out.extend(blocked)
                if not candidates and blocked and not absent:
                    # Every host the step names is here, and none can run it.
                    # With an absent host among them the step was written for
                    # a bench this is not, so it is left out instead.
                    raise PlanningError(
                        f"{step.test_name}: no selected host can run it: "
                        + "; ".join(f"{item.host}: {item.reason}" for item in blocked)
                    )
                # Then the hardware itself. A host without the device a role
                # binds is recorded, and a step no host here has the hardware
                # for is left out: the step belongs to another bench.
                bound: list[tuple[str, list]] = []
                for alias in candidates:
                    try:
                        choices = self._role_choices(
                            definition, alias, arguments, wanted
                        )
                    except MissingResource as error:
                        if strict_hardware:
                            raise
                        left_out.append(
                            NotApplicable(alias, step.test_name, str(error))
                        )
                        continue
                    bound.append((alias, choices))
                not_applicable.extend(left_out)
                if not bound:
                    continue
                # Required hardware this bench has not got, for this step.
                # Recorded per host, and the step produces no invocation for
                # that host, so the rest of the suite still compiles.
                step_blocked: list[Blocked] = []
                for host, choices in bound:
                    host_choices[host] = dict(zip(role_names, choices))
                    combinations = itertools.product(*choices) if choices else [()]
                    for selected in combinations:
                        if len(set(selected)) != len(selected):
                            continue
                        resources = MappingProxyType(dict(zip(role_names, selected)))
                        identities = MappingProxyType({
                            role: MappingProxyType(
                                dict(self.registry.get(alias).identity)
                            )
                            for role, alias in resources.items()
                        })
                        power_isolation = ()
                        exclusive = arguments.get("exclusive Ubertooth") is True
                        try:
                            if arguments.get("alternate Ubertooths") is True:
                                resources, identities = self._alternated(
                                    step, host, arguments, resources, identities
                                )
                            if definition.guards_other_ubertooths:
                                resources, identities = self._guarded(
                                    host, resources, identities
                                )
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
                        except MissingRequiredHardware as error:
                            if strict_hardware:
                                raise
                            step_blocked.append(
                                Blocked(host, step.test_name, str(error))
                            )
                            continue
                        # Numbered only once it is an invocation, so the IDs
                        # of a suite with a blocked step stay consecutive.
                        sequence += 1
                        invocations.append(
                            TestInvocation(
                                invocation_id=f"{sequence:03d}-{step.test_name}",
                                test_name=step.test_name,
                                host=host,
                                arguments=arguments,
                                resources=resources,
                                resource_identities=identities,
                                resource_placements=self._placements(resources),
                                placement_requirements=MappingProxyType({
                                    role: MappingProxyType(want.to_dict())
                                    for role, want in wanted.items()
                                }),
                                power_isolation=power_isolation,
                                exclusive_ubertooth=exclusive,
                            )
                        )
                blocked_tests.extend(step_blocked)
                if len(invocations) == step_start and not step_blocked:
                    # Every combination put one device in two roles. The
                    # message names what each role could have taken, since
                    # that is what has to change: the plan's selectors, or
                    # the host's defaults.
                    raise PlanningError(
                        f"{step.test_name}: no valid distinct resource "
                        "assignment: "
                        + "; ".join(
                            f"{host}: "
                            + ", ".join(
                                f"{role} from {' or '.join(aliases)}"
                                for role, aliases in roles.items()
                            )
                            for host, roles in host_choices.items()
                        )
                    )
                self._check_some_case_applies(
                    definition, invocations[step_start:], blocked
                )
        except RegistryError as error:
            raise PlanningError(str(error)) from error
        if not invocations:
            # Asking to run a suite this bench has nothing for is still an
            # error: the caller gets told why, test by test, rather than an
            # empty run that looks like a pass. A blocked step is named
            # first, since it is the one this bench was meant to measure.
            left_out = blocked_tests + not_applicable
            shown = left_out[:5]
            rest = len(left_out) - len(shown)
            raise PlanningError(
                f"suite '{suite}' produced no runnable invocations"
                + "".join(f"; {item.describe()}" for item in shown)
                + (f"; and {rest} more left out" if rest > 0 else "")
            )
        return CompiledSuite(
            tuple(invocations), tuple(not_applicable), tuple(sorted(absent_hosts)),
            tuple(blocked_tests),
        )

    def compile(
        self, plan: Plan, suite: str, *, strict_hardware: bool = False
    ) -> tuple[TestInvocation, ...]:
        return self.compile_suite(
            plan, suite, strict_hardware=strict_hardware
        ).invocations

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
