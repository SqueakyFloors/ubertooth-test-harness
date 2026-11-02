"""Load one-key named tests and validate arguments against the test catalog."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from ubertooth_harness import weaklink
from ubertooth_harness.catalog import CatalogError, TestCatalog, builtin_catalog


class PlanError(ValueError):
    """The test plan is missing or violates the named-test contract."""


@dataclass(frozen=True)
class TestStep:
    test_name: str
    arguments: Mapping


@dataclass(frozen=True)
class FlashStep:
    """Compatibility view consumed by the existing flash reliability runner."""

    id: str
    platform: str
    flashes: int
    image_sequence: tuple[str, ...]
    kgv_sha256: str
    required_verified_flashes: int
    allowed_command_failures: int = 0
    allowed_version_mismatches: int = 0
    allowed_manual_recoveries: int = 0
    allowed_power_cycles: int = 0
    allowed_unselected_device_changes: int = 0


def image_label(name: str) -> str:
    """Normalized image label: 'test build' -> 'test_build', 'KGV' -> 'kgv'."""
    return str(name).lower().replace(" ", "_")


def flash_step(arguments: Mapping, platform: str) -> FlashStep:
    """The flash reliability step for one platform, from plan arguments."""
    return FlashStep(
        id=f"flash-reliability-{platform}",
        platform=platform,
        flashes=arguments["flashes"],
        image_sequence=tuple(image_label(label) for label in arguments["image sequence"]),
        kgv_sha256=arguments["KGV sha256"],
        required_verified_flashes=arguments["flashes"],
    )


@dataclass(frozen=True)
class Plan:
    name: str
    suites: Mapping[str, tuple[TestStep, ...]]
    # False for a plan of planned tests (backlog.yaml, mac-polling.yaml): it
    # may name tests with no implementation yet. A runnable plan may not.
    runnable: bool = True

    def steps(self, suite: str) -> tuple[TestStep, ...]:
        try:
            return self.suites[suite]
        except KeyError as error:
            raise PlanError(f"unknown suite '{suite}'") from error

    @property
    def flash_steps(self) -> tuple[FlashStep, ...]:
        result = []
        for steps in self.suites.values():
            for step in steps:
                if step.test_name != "Flash reliability":
                    continue
                for platform in step.arguments["platforms"]:
                    result.append(flash_step(step.arguments, platform))
        return tuple(result)

    def flash_step_for(self, platform: str) -> FlashStep:
        matches = [step for step in self.flash_steps if step.platform == platform]
        if len(matches) != 1:
            raise PlanError(
                f"expected one flash reliability step for {platform}, found {len(matches)}"
            )
        return matches[0]


def load_plan(path: Path | str, catalog: TestCatalog | None = None) -> Plan:
    """Load all suites and validate every exact test name and direct argument."""
    plan_path = Path(path)
    test_catalog = catalog or builtin_catalog()
    try:
        with plan_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise PlanError(f"unable to load plan {plan_path}: {error}") from error
    if not isinstance(raw, dict):
        raise PlanError("plan root must be a mapping")
    name = raw.get("plan")
    suites = raw.get("suites")
    if not isinstance(name, str) or not name:
        raise PlanError("plan name must be a non-empty string")
    if not isinstance(suites, dict) or not suites:
        raise PlanError("plan suites must be a non-empty mapping")
    runnable = raw.get("runnable", True)
    if not isinstance(runnable, bool):
        raise PlanError("plan runnable must be true or false")

    normalized_suites = {}
    for suite_name, raw_steps in suites.items():
        if not isinstance(suite_name, str) or not suite_name:
            raise PlanError("suite name must be a non-empty string")
        if not isinstance(raw_steps, list):
            raise PlanError(f"suite '{suite_name}' must be a list")
        steps = []
        for position, raw_step in enumerate(raw_steps, 1):
            location = f"suite '{suite_name}' step {position}"
            if not isinstance(raw_step, dict) or len(raw_step) != 1:
                raise PlanError(f"{location} must contain exactly one test name")
            test_name, arguments = next(iter(raw_step.items()))
            if not isinstance(test_name, str) or not test_name:
                raise PlanError(f"{location} test name must be a non-empty string")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise PlanError(f"{location} arguments must be a mapping")
            try:
                definition = test_catalog.get(test_name)
                normalized = definition.normalize_arguments(arguments)
            except CatalogError as error:
                raise PlanError(f"{location}: {error}") from error
            steps.append(TestStep(test_name, normalized))
        normalized_suites[suite_name] = tuple(steps)
    return Plan(name, MappingProxyType(normalized_suites), runnable)


def unimplemented_tests(plan: Plan, catalog: TestCatalog | None = None) -> tuple[str, ...]:
    """Tests the plan names that have no implementation, in plan order."""
    test_catalog = catalog or builtin_catalog()
    names = []
    for steps in plan.suites.values():
        for step in steps:
            if not test_catalog.get(step.test_name).implemented and step.test_name not in names:
                names.append(step.test_name)
    return tuple(names)


def unmeasured_attenuations(
    plan: Plan, catalog: TestCatalog | None = None
) -> Mapping[str, tuple[float, ...]]:
    """Per test, the attenuations it is pointed at that nobody has measured.

    A test's pass or fail figures were set on whatever link it last ran over,
    so running it behind an attenuator the bench has never been measured at
    would judge it against figures taken somewhere else. Those entries wait in
    a plan with ``runnable: false`` until the measurement exists
    (``weaklink.MEASURED_ATTENUATIONS_DB``).
    """
    test_catalog = catalog or builtin_catalog()
    found = {}
    for steps in plan.suites.values():
        for step in steps:
            definition = test_catalog.get(step.test_name)
            asked = definition.placement_requirements(step.arguments)
            unmeasured = weaklink.unmeasured(
                want.min_attenuation_db for want in asked.values()
                if want.min_attenuation_db
            )
            if unmeasured:
                found.setdefault(step.test_name, set()).update(unmeasured)
    return MappingProxyType({
        name: tuple(sorted(values)) for name, values in found.items()
    })


def check_runnable(plan: Plan, catalog: TestCatalog | None = None) -> None:
    """Refuse a runnable plan that cannot judge what it would run.

    Two ways it cannot: a test with no implementation, whose suite run-suite
    would refuse; and a test pointed at an attenuation the bench has never
    been measured at, which would be judged against figures taken over
    another link. Both wait in a plan with ``runnable: false``
    (testplans/backlog.yaml, testplans/weak-link.yaml).
    """
    if not plan.runnable:
        return
    missing = unimplemented_tests(plan, catalog)
    if missing:
        raise PlanError(
            f"plan {plan.name} names tests with no implementation: "
            + ", ".join(missing)
            + "; move them to a plan with 'runnable: false' (testplans/backlog.yaml)"
        )
    unmeasured = unmeasured_attenuations(plan, catalog)
    if unmeasured:
        named = "; ".join(
            f"{name} at {weaklink.describe_attenuations(values)}"
            for name, values in unmeasured.items()
        )
        raise PlanError(
            f"plan {plan.name} asks for attenuation the bench has not been "
            f"measured at: {named}. The bench is measured at "
            f"{weaklink.describe_attenuations(weaklink.MEASURED_ATTENUATIONS_DB)}"
            "; until a measurement exists these wait in a plan with "
            "'runnable: false' (testplans/weak-link.yaml)"
        )
