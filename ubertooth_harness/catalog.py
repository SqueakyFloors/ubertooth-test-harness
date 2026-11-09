"""Explicit catalog of known tests and their argument/resource contracts."""

from dataclasses import dataclass, replace
import re
from types import MappingProxyType
from collections.abc import Sequence
from typing import Callable, Mapping

from ubertooth_harness.cases import CommandCase, FlashCase, guard_roles
from ubertooth_harness.hostneeds import (
    ALWAYS_BUILT_TOOLS, BLUEZ_TOOLS, NO_NEEDS, PLATFORMS, Needs, tool_needs,
)
from ubertooth_harness.rf import (
    NORMAL_LINK, PlacementError, PlacementRequirement,
    requirement as rf_requirement,
    requirements as rf_requirements,
)
from ubertooth_harness import weaklink
from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    FIRMWARE_PATTERN,
    HOST_VERSION_LINE,
    HOST_VERSION_TIMEOUT_S,
    PART_ID_PATTERN,
    VERSION_ANSWER_PATTERNS,
    serial_pattern,
)
from ubertooth_harness.outputfiles import output_of
from ubertooth_harness.resources import BLUETOOTH_ADAPTER, ESP32_CLASSIC_PEER
from ubertooth_harness.stateful import StateCaseError, coverage_profile
from ubertooth_harness import (
    addresssoak, advertiser_cases, advertiser_soak_cases, afh_cases,
    bluez_cases, boardstate_cases, btbr_cases,
    btle_replay_cases,
    capture_failure_cases, classic_cases, connection_cases, dfu_cases,
    dfu_host_cases, follow_soak_cases, followsoak, interrupt_cases,
    invalidapp_cases, python_cases,
    receive_cases,
    refusal_cases, tool_cases, transitions, tshark_cases, util_cases,
    util_refusal_cases,
)


class CatalogError(ValueError):
    """A test name or argument does not match the catalog."""


# The plan argument that turns the firmware's self-reset count into a
# pass/fail measure for one test (watchdog.py).
WATCHDOG_LIMIT_ARGUMENT = "max_watchdog_resets"

# The plan argument that makes one step's hardware part of what the step
# measures. Where a step carries it and this registry has not got that
# hardware, the step is recorded blocked and the run fails, rather than being
# left out as not applicable (planner.py, hostneeds.Blocked).
REQUIRED_HARDWARE_ARGUMENT = "required hardware"

_MISSING = object()
_DURATION_RE = re.compile(r"^([1-9][0-9]*)(ms|s|m|h)$")


def _hosts(value):
    if value == "each":
        return value
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(item, str) and item for item in value
    ):
        return tuple(value)
    raise CatalogError("hosts must be 'each' or a non-empty host alias list")


def _selector(value):
    if isinstance(value, str) and value:
        if value in ("any", "random"):
            raise CatalogError(f"ambiguous hardware selector '{value}' is unsupported")
        return value
    raise CatalogError("hardware selector must be default, each, or an explicit alias")


def _platforms(value):
    if not isinstance(value, (list, tuple)) or not value:
        raise CatalogError("platforms must be a non-empty list")
    normalized = tuple(value)
    if any(item not in PLATFORMS for item in normalized):
        raise CatalogError("platforms may contain only " + " and ".join(PLATFORMS))
    if len(set(normalized)) != len(normalized):
        raise CatalogError("platforms must not contain duplicates")
    return normalized


# Flash-and-session cycles `Classic btbr console` will run. Each writes the
# board's firmware twice, so the ceiling is what the `btbr-long` suite needs
# with room above it, not a limit of the code.
MAX_BTBR_SESSIONS = 20


def _btbr_sessions(value):
    count = _positive_integer(value)
    if count > MAX_BTBR_SESSIONS:
        raise CatalogError(
            f"sessions must be 1-{MAX_BTBR_SESSIONS} (got {count}): each one "
            "writes the board's firmware twice")
    return count


def _positive_integer(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CatalogError("must be a positive integer")
    return value


def _nonnegative_integer(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CatalogError("must be a nonnegative integer")
    return value


def _duration(value):
    if not isinstance(value, str) or not _DURATION_RE.fullmatch(value):
        raise CatalogError("duration must look like 500ms, 10s, 15m, or 1h")
    return value


def _sequence(value):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise CatalogError("image sequence must contain at least two labels")
    normalized = tuple(value)
    if not all(isinstance(item, str) and item for item in normalized):
        raise CatalogError("image sequence labels must be non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise CatalogError("image sequence labels must be distinct")
    return normalized


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        raise CatalogError("KGV sha256 must be 64 hexadecimal characters")
    try:
        int(value, 16)
    except ValueError as error:
        raise CatalogError("KGV sha256 must be hexadecimal") from error
    return value.lower()


def _register(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFF:
        return value
    if isinstance(value, str) and re.fullmatch(r"0x[0-9A-Fa-f]{1,2}", value):
        return int(value, 16)
    raise CatalogError("register must be an integer or 0x00-0xff")


def _boolean(value):
    if not isinstance(value, bool):
        raise CatalogError("must be true or false")
    return value


def _required_true(value):
    if value is not True:
        raise CatalogError("must be true")
    return True


def _required_text(value):
    if not isinstance(value, str) or not value:
        raise CatalogError("must be a non-empty string")
    return value


def _pa_level(value):
    # FREND.PA_LEVEL: 0 (-25 dBm) to 7 (0 dBm).
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 7:
        raise CatalogError("must be a PA level 0-7")
    return value


def _channel_mhz(value):
    # The range ubertooth-util -c takes.
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 2402 <= value <= 2480):
        raise CatalogError("must be a channel of 2402-2480 MHz")
    return value


def _pa_levels(value):
    """An ascending list of distinct PA levels, as a power curve needs them."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise CatalogError("must be a list of at least two PA levels 0-7")
    levels = tuple(_pa_level(item) for item in value)
    if len(set(levels)) != len(levels):
        raise CatalogError("must not repeat a PA level")
    if list(levels) != sorted(levels):
        raise CatalogError("must list PA levels in ascending order")
    return levels


def _advertising_interval(value):
    # Bluetooth Core: legacy advertising interval 20 ms to 10.24 s.
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 20 <= value <= 10240):
        raise CatalogError("must be an advertising interval of 20-10240 ms")
    return value


def _connection_interval(value):
    # The role firmware's range (Bluetooth Core: 7.5 ms to 4 s).
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 8 <= value <= 4000):
        raise CatalogError("must be a connection interval of 8-4000 ms")
    return value


def _write_bytes(value):
    # What the central writes each time the link is up. The firmware's own
    # ceiling is one link-layer packet's worth with Data Length Extension,
    # which is what `Capture known BLE connection` uses as its constant
    # (connection_cases.WRITE_BYTES); a larger value is two packets and no
    # longer what a "long packet" check is about.
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 1 <= value <= connection_cases.WRITE_BYTES):
        raise CatalogError(
            f"must be 1-{connection_cases.WRITE_BYTES} bytes, which is what one "
            "link-layer packet carries with Data Length Extension")
    return value


def _check_connection_duration(arguments) -> None:
    """The capture must span exactly one of the central's reconnections.

    One, so it holds a CONNECT_IND and the connection after it; not two,
    because a longer capture has been seen to die partway through
    (connection_cases.CYCLE_MS says why).
    """
    cycle_s = connection_cases.CYCLE_MS / 1000.0
    seconds = receive_cases.seconds_arg(arguments)
    if not cycle_s < seconds < 2 * cycle_s:
        raise CatalogError(
            f"duration must be longer than {cycle_s:g}s and shorter than "
            f"{2 * cycle_s:g}s, so the capture holds one of the central's "
            "reconnections and not two")


# The longest capture the multi-cycle follow case takes. Three captures of
# this plus the DK setup is a couple of minutes, which keeps the case inside
# a short hardware session.
_MAX_FOLLOW_END_CAPTURE_S = 30


def _check_follow_end_duration(arguments) -> None:
    """The capture must span several of the central's reconnections.

    At least MIN_CYCLES of them, which is what the test is for, and not so
    many that one case holds the boards for long: the capture is taken up to
    CAPTURE_ATTEMPTS times.
    """
    cycle_s = connection_cases.CYCLE_MS / 1000.0
    least = connection_cases.MIN_CYCLES * cycle_s
    most = _MAX_FOLLOW_END_CAPTURE_S
    seconds = receive_cases.seconds_arg(arguments)
    if not least < seconds <= most:
        raise CatalogError(
            f"duration must be longer than {least:g}s, so the capture spans "
            f"{connection_cases.MIN_CYCLES} of the central's connections, and "
            f"at most {most:g}s, so one case does not hold the boards for long")


def _advertising_channel(value):
    # The three primary advertising channels ubertooth-btle -A takes. The
    # type is checked as well: `channel: 37.0` is a float in YAML and would
    # otherwise reach the tool as -A37.0.
    if (not isinstance(value, int) or isinstance(value, bool)
            or value not in (37, 38, 39)):
        raise CatalogError("must be an advertising channel 37, 38 or 39")
    return value


def _share(value):
    """A share of something, as a fraction above 0 and at most 1.

    Written as a fraction rather than a percentage because that is how the
    run reports it, so a plan and a result read the same way.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError("must be a fraction above 0 and at most 1")
    number = float(value)
    if not 0 < number <= 1:
        raise CatalogError(f"must be a fraction above 0 and at most 1 (got {value})")
    return number


def _check_follow_soak(arguments) -> None:
    """Refuse follow-soak figures that could not judge what they ask for.

    The same kind of check as `_check_address_soak` and for the same reason:
    the run leases an Ubertooth and two DKs and then holds them for hours, so
    a plan whose figures contradict each other has to fail before any of
    that. The rules themselves are the driver's
    (followsoak.check_timings), read through the same conversion the driver's
    own command line gets, so the plan is judged by what will really run.
    """
    if arguments["capture file limit"] > arguments["capture budget"]:
        raise CatalogError(
            f"'capture file limit' ({arguments['capture file limit']} MiB) is "
            f"larger than 'capture budget' ({arguments['capture budget']} MiB), "
            "which is what the whole run's captures may cost")
    try:
        followsoak.check_timings(follow_soak_cases.timings(arguments))
    except followsoak.SoakError as error:
        raise CatalogError(str(error)) from error


def _check_address_soak(arguments) -> None:
    """Refuse soak figures that could not judge what they ask for.

    Each of these is a relationship between two arguments -- a detection
    deadline longer than the advertiser is on for, a liveness timeout the
    schedule's own silence would trip, a gap limit every period crosses --
    and they are checked when the plan loads rather than on the bench: the
    run leases an Ubertooth and a DK and then holds them for hours, so a
    plan whose figures contradict each other has to fail before any of that.
    The rules themselves are the driver's (addresssoak.check_timings), read
    through the same conversion the driver's own command line gets, so the
    plan is judged by what will really run.
    """
    if arguments["capture file limit"] > arguments["capture budget"]:
        # The budget is what the run's captures may cost altogether, so a
        # per-file limit above it would prune down to one over-budget file
        # at every rotation. Here rather than in the driver because it is a
        # relationship between two plan figures, like the rest.
        raise CatalogError(
            f"'capture file limit' ({arguments['capture file limit']} MiB) is "
            f"larger than 'capture budget' ({arguments['capture budget']} MiB), "
            "which is what the whole run's captures may cost")
    try:
        addresssoak.check_timings(advertiser_soak_cases.timings(arguments))
    except addresssoak.SoakError as error:
        raise CatalogError(str(error)) from error


def _payload_tag(value):
    # The advertiser firmware's payload tag: up to 12 bytes, or none.
    text = str(value).lower() if isinstance(value, (str, int)) else None
    if text == "none":
        return ""
    if text is None or not re.fullmatch(r"(?:[0-9a-f]{2}){1,12}", text):
        raise CatalogError("must be 1-12 bytes of hex, or none")
    return text


def _coverage(value):
    try:
        return coverage_profile(value)
    except ValueError as error:
        raise CatalogError(str(error)) from error


def _rf(value):
    try:
        asked = rf_requirements(value)
    except PlacementError as error:
        raise CatalogError(str(error)) from error
    # Normalized arguments are written to the manifest and run.jsonl as JSON,
    # and are read back from a manifest (the GUI), so a requirement is kept
    # as the plain record it validated to; the planner reads it back with
    # placement_requirements().
    return MappingProxyType({
        role: MappingProxyType(want.to_dict()) for role, want in asked.items()
    })


def _weak_link(value):
    try:
        asked = weaklink.weak_link(value)
    except PlacementError as error:
        raise CatalogError(str(error)) from error
    # Kept as plain numbers for the same reason `rf:` is kept as plain
    # records: the normalized arguments are written to the manifest and
    # run.jsonl as JSON and read back from there.
    return MappingProxyType(dict(asked))


@dataclass(frozen=True)
class ArgumentRule:
    validator: Callable
    default: object = _MISSING
    required: bool = False


# Arguments whose value is how long a test (or its capture) runs.
DURATION_ARGUMENTS = ("duration", "capture duration")
# Estimate key of a test without a coverage argument.
DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class Estimate:
    """Expected wall time of one test invocation, in seconds.

    ``fixed_s``, plus ``per_duration`` times the test's duration argument
    (``duration`` or ``capture duration``), plus ``per_count[1]`` seconds for
    each unit of the integer argument named ``per_count[0]`` (for example
    seconds per flash). Build, preflight and planning are not included.
    """

    fixed_s: float
    per_duration: float = 0.0
    per_count: tuple[str, float] | None = None

    def seconds(self, arguments: Mapping) -> float | None:
        """The estimate, or None when an argument it scales by is absent."""
        total = self.fixed_s
        if self.per_duration:
            duration = _duration_argument_seconds(arguments)
            if duration is None:
                return None
            total += self.per_duration * duration
        if self.per_count is not None:
            name, each = self.per_count
            count = arguments.get(name)
            if isinstance(count, bool) or not isinstance(count, int):
                return None
            total += each * count
        return round(total, 1)


def _duration_argument_seconds(arguments: Mapping) -> float | None:
    """The first duration argument in seconds; None if absent or invalid."""
    for name in DURATION_ARGUMENTS:
        if arguments.get(name) is not None:
            try:
                return _duration_seconds(arguments[name])
            except CatalogError:
                return None
    return None


@dataclass(frozen=True)
class TestDefinition:
    name: str
    description: str
    arguments: Mapping[str, ArgumentRule]
    hardware_roles: Mapping[str, str] = None
    argv: tuple[str, ...] | None = None
    timeout_s: float = 5.0
    output_pattern: str | None = None
    expected_returncodes: tuple[int, ...] = (0,)
    command_builder: Callable | None = None
    continue_on_case_failure: bool = False
    # The test writes the firmware it then verifies, so preflight does not
    # require the resolved revision on its Ubertooth beforehand.
    establishes_firmware: bool = False
    # The test writes firmware to a board, so run-suite holds the host lock
    # (leases.py) for the run: no power change or other write may overlap it.
    writes_firmware: bool = False
    # The test can leave a board that needs someone at the bench: it is run
    # on purpose, by a person, with `run-suite --allow-destructive`, and
    # never by a dispatched run (the runner does not pass that option on, so
    # the GUI cannot start it).
    destructive: bool = False
    # A case of the test switches the selected board's USB power off and on
    # through its registered controller. The planner resolves the port from
    # the board's own registry mapping (planner.TestInvocation.power_cycle),
    # run-suite requires --allow-power-changes, and the run holds the host
    # lock for the whole test, as power isolation does.
    switches_power: bool = False
    # Cross-argument rule checked on the normalized arguments (no registry).
    check_arguments: Callable[[Mapping], None] | None = None
    # The planner binds every other registered Ubertooth on the host as a
    # "guard Ubertooth N" resource; the sequencer reads each guard before the
    # test and fails the test if any read changes.
    guards_other_ubertooths: bool = False
    # What every case of the test needs from the host (hostneeds.py). The
    # plan's `platforms:` argument adds a platform need (test_needs).
    needs: Needs = NO_NEEDS
    # What SOME of the test's cases need from the run's build: the firmware
    # image ("test build"), the alternate btbr image ("btbr build"), the
    # Python tools, or several of them; without it those cases are left out
    # as not applicable. Only those three fields of Needs belong here and in
    # build_required.
    #
    # Unlike `needs`, which leaves a whole test out where its host does not
    # meet it, these two are how a run's build options are chosen: the cases
    # are built from the host's own registry resources, so a client without
    # one (the controller's plan listing, and so the GUI) cannot read the
    # needs off the cases themselves.
    build_needs: Needs = NO_NEEDS
    # What the test cannot run at all without: its cases name the run's own
    # image in their artifacts (or its command needs one), so a run without
    # it is refused rather than leaving a case out. Implies build_needs.
    build_required: Needs = NO_NEEDS
    # The argument that names the firmware images the run must carry, for a
    # test whose plan entry decides them (Flash reliability's `image
    # sequence`). The run's own build makes "test build"; every other label
    # has to come from --artifact, which only the command line passes, so a
    # client that dispatches runs (the GUI) reads the labels from the plan
    # rather than assuming which ones a step names. See image_labels.
    image_labels_argument: str | None = None
    # Expected wall time by coverage profile (DEFAULT_PROFILE for a test
    # without a coverage argument); see ESTIMATES.
    estimates: Mapping[str, Estimate] | None = None
    # The RF placement each hardware role needs when the plan does not say
    # (rf.py): a capture or a survey asks for NORMAL_LINK, so the planner
    # keeps it off an attenuated board. The plan's `rf:` overrides it per role.
    rf_defaults: Mapping[str, PlacementRequirement] | None = None

    def __post_init__(self):
        if (self.image_labels_argument
                and self.image_labels_argument not in self.arguments):
            raise ValueError(
                f"{self.name}: image_labels_argument "
                f"{self.image_labels_argument!r} is not one of its arguments"
            )
        for field, declared in (("build_needs", self.build_needs),
                                ("build_required", self.build_required)):
            unwanted = Needs(
                platforms=declared.platforms,
                bluez_tools=declared.bluez_tools,
                host_tools=declared.host_tools,
            )
            if unwanted:
                raise ValueError(
                    f"{self.name}: {field} carries build options only "
                    f"(python_tools, firmware_build, btbr_build), not "
                    f"{unwanted.describe()}"
                )

    def build_options(self) -> Needs:
        """The run build options some case of this test needs.

        Its own test-wide needs, what its cases need, and what it cannot run
        without at all. Each artifact a case needs is named here in its own
        right: the alternate btbr image is not read off the firmware build,
        because a run can carry the application image without it (#279).
        """
        return Needs(
            python_tools=(self.needs.python_tools or self.build_needs.python_tools
                          or self.build_required.python_tools),
            firmware_build=(self.needs.firmware_build or self.build_needs.firmware_build
                            or self.build_required.firmware_build),
            btbr_build=(self.needs.btbr_build or self.build_needs.btbr_build
                        or self.build_required.btbr_build),
        )

    def required_build_options(self) -> Needs:
        """The run build options without which this test cannot run at all."""
        return Needs(
            python_tools=self.build_required.python_tools,
            firmware_build=self.build_required.firmware_build,
            btbr_build=self.build_required.btbr_build,
        )

    def image_labels(self, arguments: Mapping | None = None) -> tuple[str, ...]:
        """The firmware images these arguments say the run must carry.

        As the plan entry writes them, so a caller can quote one in the
        ``--artifact "LABEL=PATH"`` a run needs. Empty for a test whose
        images do not come from an argument, and for arguments that do not
        carry that one (a plan listing, which reads a step as written rather
        than normalized).
        """
        if not self.image_labels_argument:
            return ()
        listed = (arguments or {}).get(self.image_labels_argument)
        if not isinstance(listed, Sequence) or isinstance(listed, str):
            return ()
        return tuple(item for item in listed if isinstance(item, str) and item)

    def placement_requirements(
        self, arguments: Mapping
    ) -> Mapping[str, PlacementRequirement]:
        """The placement every hardware role must satisfy for these arguments.

        The plan's ``rf:`` for a role, else what its ``weak link:`` makes of
        that role, else the test's own default for it; roles with none of the
        three are absent, and any resource may take them.

        ``rf:`` and ``weak link:`` cannot both be set (normalize_arguments
        refuses it), so the first two are never in play at once.
        """
        requested = arguments.get("rf") or {}
        weak = weaklink.placements(
            arguments.get("weak link") or {}, self.hardware_roles
        )
        defaults = self.rf_defaults or {}
        wanted = {}
        for role in self.hardware_roles or {}:
            if role in requested:
                # Normalizing the argument validated this record, so reading
                # it back cannot fail.
                placement = rf_requirement(requested[role])
            elif role in weak:
                placement = weak[role]
            else:
                placement = defaults.get(role)
            if placement:
                wanted[role] = placement
        return MappingProxyType(wanted)

    def estimate_profile(self, arguments: Mapping) -> str | None:
        """Which calibrated estimate these normalized arguments select.

        The step's coverage profile, or `DEFAULT_PROFILE` for a test that
        takes no `coverage:`. None for a test with no calibrated estimates,
        whose expected time is its duration argument instead. One place,
        because `estimated_seconds` and the plans listing both have to name
        the same profile for the same step (#315).
        """
        if not self.estimates:
            return None
        return arguments.get("coverage", DEFAULT_PROFILE)

    def estimated_seconds(self, arguments: Mapping) -> float | None:
        """Expected seconds of one invocation with these normalized arguments.

        A test without calibrated estimates (no implementation yet) is
        expected to run for its duration argument, if it has one. None, never
        an error, when there is no estimate for these arguments (for example
        a per-duration estimate without a duration argument).
        """
        profile = self.estimate_profile(arguments)
        if profile is not None:
            estimate = self.estimates.get(profile)
            return None if estimate is None else estimate.seconds(arguments)
        return _duration_argument_seconds(arguments)

    def test_needs(self, arguments: Mapping) -> tuple[Needs, ...]:
        """The test-wide needs: the definition's and the plan's platforms.

        A step without `platforms:` adds no platform need, so it runs on
        every platform.
        """
        needs = [self.needs] if self.needs else []
        if arguments.get("platforms"):
            needs.append(Needs(platforms=tuple(arguments["platforms"])))
        return tuple(needs)

    def normalize_arguments(self, raw: Mapping | None) -> Mapping:
        values = dict(raw or {})
        unknown = sorted(set(values) - set(self.arguments))
        if unknown:
            raise CatalogError(
                f"{self.name}: unknown argument(s): {', '.join(unknown)}"
            )
        normalized = {}
        for name, rule in self.arguments.items():
            if name in values:
                try:
                    normalized[name] = rule.validator(values[name])
                except CatalogError as error:
                    raise CatalogError(f"{self.name}.{name}: {error}") from error
            elif rule.default is not _MISSING:
                normalized[name] = rule.default
            elif rule.required:
                raise CatalogError(f"{self.name}: missing required argument '{name}'")
        for name in ("rf", "weak link"):
            if name not in normalized:
                continue
            # A placement requirement selects the resource bound to one
            # hardware role, so a role the test does not have could never be
            # applied.
            unknown = sorted(set(normalized[name]) - set(self.hardware_roles or {}))
            if unknown:
                raise CatalogError(
                    f"{self.name}.{name}: unknown hardware role(s) "
                    f"{', '.join(unknown)}; this test binds "
                    + (", ".join(self.hardware_roles or {}) or "no hardware")
                )
        if "rf" in normalized and "weak link" in normalized:
            # They say the same thing in two ways, and `weak link:` speaks for
            # every role of the test, so which one wins per role would be a
            # puzzle rather than a choice.
            raise CatalogError(
                f"{self.name}: set rf: or weak link:, not both; weak link: "
                "already requires an ordinary board for every other role"
            )
        if self.check_arguments is not None:
            try:
                self.check_arguments(normalized)
            except CatalogError as error:
                raise CatalogError(f"{self.name}: {error}") from error
        return MappingProxyType(normalized)

    @property
    def implemented(self) -> bool:
        return self.argv is not None or self.command_builder is not None

    def command_cases(
        self,
        arguments: Mapping,
        resources: Mapping[str, str],
        resource_identities: Mapping[str, Mapping[str, str]],
    ) -> tuple[CommandCase, ...]:
        if self.command_builder is not None:
            try:
                cases = tuple(
                    self.command_builder(arguments, resources, resource_identities)
                )
            except StateCaseError as error:
                raise CatalogError(f"{self.name}: {error}") from error
            if not cases:
                raise CatalogError(f"{self.name}: implementation produced no cases")
            if len({case.case_id for case in cases}) != len(cases):
                raise CatalogError(f"{self.name}: duplicate command case ID")
            return cases
        command = self.command(arguments)
        patterns = (self.output_pattern,) if self.output_pattern else ()
        return (
            CommandCase(
                "command-1",
                self.name,
                command,
                patterns,
                self.timeout_s,
                expected_returncodes=self.expected_returncodes,
            ),
        )

    def command(self, arguments: Mapping) -> tuple[str, ...]:
        if self.argv is None:
            raise CatalogError(f"{self.name}: execution is not implemented")
        return self.argv

    def result_failure(self, result) -> str:
        if result.timed_out:
            return f"{self.name}: command timed out"
        if result.cancelled:
            return f"{self.name}: command canceled"
        if result.returncode not in self.expected_returncodes:
            return f"{self.name}: command exited {result.returncode}"
        if self.output_pattern:
            output = output_of(result)
            match = output.search(self.output_pattern)
            reason = (
                "" if match else
                f"{self.name}: expected output pattern "
                f"{self.output_pattern!r} was not found"
            )
            output.note_pattern("expected", self.name, self.output_pattern,
                                match, reason=reason)
            return reason
        return ""


class TestCatalog:
    def __init__(self, definitions):
        by_name = {}
        for definition in definitions:
            if definition.name in by_name:
                raise CatalogError(f"duplicate test name '{definition.name}'")
            by_name[definition.name] = definition
        self._definitions = MappingProxyType(by_name)

    def get(self, name: str) -> TestDefinition:
        try:
            return self._definitions[name]
        except KeyError as error:
            raise CatalogError(f"unknown test '{name}'") from error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)


HOSTS = ArgumentRule(_hosts, default="each")
UBERTOOTH = ArgumentRule(_selector, default="default")
# Every test accepts `max_watchdog_resets:` (builtin_catalog adds it), so a
# stability run can make the firmware's self-reset count a pass/fail measure.
# It is optional: a step that leaves it out has no limit, and a self-reset is
# only reported (watchdog.py), so the normalized arguments carry it only when
# the plan sets it.
MAX_WATCHDOG_RESETS = ArgumentRule(_nonnegative_integer)
# Every test accepts `platforms:` (builtin_catalog adds it where the test does
# not declare its own rule). It is optional: a step that leaves it out runs on
# every platform, so the normalized arguments carry it only when the plan does.
PLATFORMS_ARGUMENT = ArgumentRule(_platforms)
# Every test that binds hardware accepts `rf:` (builtin_catalog adds it), one
# placement requirement per hardware role. Optional, like `platforms:`.
RF_ARGUMENT = ArgumentRule(_rf)
# ... and `weak link:`, the shorthand for it: one role at an attenuation in
# dB, with every other role an ordinary board (weaklink.py). Also optional.
WEAK_LINK_ARGUMENT = ArgumentRule(_weak_link)
# Every test that binds hardware accepts `required hardware:` (builtin_catalog
# adds it), so a plan can say that one step's hardware is what the step is
# there to measure. Optional, like `platforms:`: a step that leaves it out,
# or writes it false, is left out where this registry has not got its
# hardware, as before.
REQUIRED_HARDWARE = ArgumentRule(_boolean)
DURATION = ArgumentRule(_duration, required=True)
SAMPLE_EVERY = ArgumentRule(_duration, required=True)
COVERAGE = ArgumentRule(_coverage, required=True)


def _device_test(name, description, argv=None, extra=None):
    arguments = {"hosts": HOSTS, "Ubertooth": UBERTOOTH}
    arguments.update(extra or {})
    return TestDefinition(
        name,
        description,
        MappingProxyType(arguments),
        MappingProxyType({"Ubertooth": "Ubertooth"}),
        argv,
    )


# ubertooth_print_host_version() in libubertooth, the first line of every
# tool's -V answer: patterns.HOST_VERSION_LINE, which is the one statement of
# the line's layout. Anchored to its label, so the board's firmware line
# under it cannot stand in for it. Which versions a given commit must print
# is decided where the resolved commit is known (preflight.py); a case has
# only the tool in front of it.
_HOST_VERSION_PATTERN = HOST_VERSION_LINE
# That line needs no board, but the command goes on to read every attached
# one and exits 1 if any cannot be read -- in use by another program, or not
# answering -- which may be a board this run does not use. The line is what
# these cases check, as it is in preflight (preflight.py).
HOST_VERSION_RETURNCODES = (0, 1)

# Every host tool `Host CLI surface` runs, with what each one needs from the
# host: the tools the fork always builds, then the two it builds only with
# BlueZ. The list is the build's own (hostneeds.ALWAYS_BUILT_TOOLS), so a
# tool the fork adds arrives here with the build rather than being added to
# a second list by hand.
HOST_CLI_TOOLS = tuple(
    (tool, tool_needs(tool))
    for tool in ALWAYS_BUILT_TOOLS + BLUEZ_TOOLS
)


def _host_cli_surface_cases(_arguments, _resources, _resource_identities):
    """`<tool> -h` and `<tool> -V` for every host tool the fork builds.

    Both run over one list of tools (``HOST_CLI_TOOLS``), so a tool cannot
    be asked its usage and not its version: `-V` used to be asked of four of
    the thirteen. What was read at Ubertooth 2026-11-DEV1, and no further
    back: every one of the thirteen sources carries `V` in its `getopt`
    option string and a `case 'V'` that calls
    ``ubertooth_print_versions()`` after the option loop and returns its
    status, so `-V` ends the command in all of them; and the eleven a macOS
    build produces were built from that tag and run, each printing the same
    host version line and exiting 0 with no board attached
    ([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).
    These cases have no `-H` fallback, so against a fork from before the one
    `-V` answer (ubertooth#813) all thirteen fail: the host version came from
    `-H` there and `-V` was the board's compile info, and the pattern asks
    for the `Host tools version:` line. Preflight's own host version check
    does have the fallback (preflight.HOST_VERSION_FALLBACK_ARGV).

    The two BlueZ-only tools carry their host need on both cases, so a host
    without BlueZ leaves out each one's help and version together.
    """
    cases = []
    for tool, needs in HOST_CLI_TOOLS:
        label = " (BlueZ builds)" if needs else ""
        cases.append(
            CommandCase(
                f"{tool}-help",
                f"{tool} -h{label}",
                (tool, "-h"),
                (re.escape(tool),),
                # -h exits 0 on every tool: btle since fork e4f1dc5 (fork
                # #377), dfu, scan and follow since 12211ce (fork #406);
                # each was 1.
                expected_returncodes=(0,),
                needs=needs,
            )
        )
    for tool, needs in HOST_CLI_TOOLS:
        label = " (BlueZ builds)" if needs else ""
        cases.append(
            CommandCase(
                f"{tool}-version",
                f"{tool} -V{label}",
                (tool, "-V"),
                (_HOST_VERSION_PATTERN,),
                timeout_s=HOST_VERSION_TIMEOUT_S,
                expected_returncodes=HOST_VERSION_RETURNCODES,
                needs=needs,
            )
        )
    return tuple(cases)


def _ubertooth_count_cases(_arguments, _resources, resource_identities):
    """ubertooth-util -N counts every attached board: the selected one plus
    every other registered board (bound as guards) and nothing else."""
    expected = 1 + len(guard_roles(resource_identities))
    return (
        CommandCase(
            "ubertooth-count",
            f"ubertooth-util -N reports {expected} attached board(s)",
            ("ubertooth-util", "-N"),
            (rf"(?m)^{expected}$",),
        ),
    )


def _debug_register_cases(arguments, _resources, resource_identities):
    try:
        serial = resource_identities["Ubertooth"]["serial"].lower()
    except KeyError as error:
        raise CatalogError(
            "Debug register read requires a registered serial"
        ) from error
    register = arguments["register"]
    return (
        CommandCase(
            "register-read",
            f"CC2400 register 0x{register:02x}",
            (
                "ubertooth-debug",
                "-U",
                serial,
                "-r",
                f"0x{register:02x}",
            ),
            (r"(?m)^%(?:[A-Z0-9]+|r[0-9A-Fa-f]{2}) = 0x[0-9A-Fa-f]{4}$",),
        ),
    )


def _ubertooth_identity_cases(_arguments, _resources, resource_identities):
    try:
        serial = resource_identities["Ubertooth"]["serial"]
    except KeyError as error:
        raise CatalogError(
            "Ubertooth identity requires a registered serial"
        ) from error
    common = ("ubertooth-util", "-U", serial.lower())
    firmware = FIRMWARE_PATTERN
    serial_output = serial_pattern(serial)
    part = PART_ID_PATTERN
    board = BOARD_ID_PATTERN
    return (
        CommandCase(
            "firmware-version", "Firmware version", common + ("-v",), (firmware,)
        ),
        # -V is the version command, not an identity query: since fork
        # f898fe5 (SqueakyFloors/ubertooth#813) it answers with the host
        # tools' version and the board's firmware version, then
        # ends the command. So it is read on its own, and the composition
        # below asks for the four queries that do compose.
        CommandCase(
            "firmware-compile-info",
            "Firmware compile info",
            common + ("-V",),
            VERSION_ANSWER_PATTERNS,
            timeout_s=HOST_VERSION_TIMEOUT_S,
        ),
        CommandCase(
            "device-serial", "Device serial", common + ("-s",), (serial_output,)
        ),
        CommandCase("part-id", "Part ID", common + ("-p",), (part,)),
        CommandCase("board-id", "Board ID", common + ("-b",), (board,)),
        CommandCase(
            "complete-readback-composition",
            "Complete readback composition",
            common + ("-vspb",),
            (firmware, serial_output, part, board),
        ),
    )


def _duration_seconds(value: str) -> float:
    match = _DURATION_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise CatalogError(f"invalid duration '{value}'")
    number, unit = int(match.group(1)), match.group(2)
    return number * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def _seconds_argument(seconds: float) -> str:
    """Fixed-point seconds for coreutils timeout (never exponent notation)."""
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


_MAX_EXCLUSIVE_CAPTURE_S = 600


def _exclusive_topology_cases(arguments, _resources, resource_identities):
    """No-selector commands must see exactly the selected board.

    With more than one Ubertooth enumerated and no -U, libubertooth prints
    "error: multiple Ubertooth devices found; select one with -U
    <index|serial>" and indented "  Device N: <serial>" lines to stderr and
    opens no device, so ubertooth-util and ubertooth-btle exit non-zero.
    These cases only pass when isolation left one board.
    """
    try:
        serial = resource_identities["Ubertooth"]["serial"].lower()
    except KeyError as error:
        raise CatalogError(
            "Exclusive Ubertooth topology requires a registered serial"
        ) from error
    seconds = _duration_seconds(arguments["capture duration"])
    if seconds > _MAX_EXCLUSIVE_CAPTURE_S:
        # Every other board stays powered off for the whole capture.
        raise CatalogError(
            "Exclusive Ubertooth topology capture duration must be at most "
            f"{_MAX_EXCLUSIVE_CAPTURE_S // 60:g}m"
        )
    return (
        CommandCase(
            "no-selector-serial",
            "No-selector serial is the selected board",
            ("ubertooth-util", "-s"),
            (rf"(?m)^Serial No:\s+{re.escape(serial)}\s*$",),
        ),
        CommandCase(
            "no-selector-firmware",
            "No-selector firmware query reaches one board",
            ("ubertooth-util", "-v"),
            (r"(?m)^Firmware version:\s+\S+",),
        ),
        CommandCase(
            "no-selector-btle-follow",
            "ubertooth-btle -f without -U keeps running on the only board",
            # coreutils timeout stops the capture and exits 124; an early exit
            # (for example, more than one device) returns another code. This
            # proves btle kept running, not that it captured packets.
            # --foreground stops timeout from moving itself and btle into a
            # new process group, so the executor's killpg of this command's
            # group reaches them on cancel or deadline; -k escalates to SIGKILL if
            # btle ignores SIGINT. ubertooth-btle prints thousands of lines a
            # second, so only the last lines are kept for the raw log; the
            # exit code is the capture's, not tail's.
            (
                "bash",
                "-c",
                f"timeout --foreground -k 2 -s INT {_seconds_argument(seconds)} ubertooth-btle -f "
                "| tail -n 20; exit ${PIPESTATUS[0]}",
            ),
            timeout_s=seconds + 10,
            expected_returncodes=(124,),
        ),
    )


def _check_alternation(arguments):
    if not arguments.get("alternate Ubertooths"):
        return
    if arguments.get("hosts", "each") == "each":
        # Planning covers every selected host; one that cannot alternate
        # would stop the run on all of them.
        raise CatalogError("alternate Ubertooths needs hosts: [...], not each")
    if arguments.get("Ubertooth") == "each":
        raise CatalogError(
            "alternate Ubertooths already flashes every board; select one "
            "Ubertooth (default or an alias), not each"
        )


def _flash_reliability_cases(arguments, _resources, resource_identities):
    try:
        serial = resource_identities["Ubertooth"]["serial"].lower()
    except KeyError as error:
        raise CatalogError("Flash reliability requires a registered serial") from error
    return (FlashCase("flash-reliability", "Flash reliability", arguments, serial),)


def _profiles(smoke: Estimate, comprehensive: Estimate) -> Mapping[str, Estimate]:
    return MappingProxyType({"smoke": smoke, "comprehensive": comprehensive})


def _default(estimate: Estimate) -> Mapping[str, Estimate]:
    return MappingProxyType({DEFAULT_PROFILE: estimate})


# Expected wall time of every implemented test, for the GUI countdown.
# Calibrated from run records on a macOS host with two Ubertooths on the
# release plan's firmware (bluetooth_rxtx, API 1.14): comprehensive values are
# the release plan run at fork 7bb07c9 (every test comprehensive, duration
# 3s); smoke values are the median of the recorded smoke runs, or, for a
# test with no smoke record, the comprehensive time per step scaled to the
# smoke steps. per_duration counts the steps that run for the duration
# argument (continuous modes stopped at it, self-timed -t captures). A
# failing test can end early, and a Linux host or a single board differs,
# so these are estimates for a countdown, never a timeout.
ESTIMATES: Mapping[str, Mapping[str, Estimate]] = MappingProxyType({
    "Host version": _default(Estimate(0.5)),
    "Help smoke": _default(Estimate(0.5)),
    # 15 cases took 3.6 s on the calibration host, so 0.24 s a case; asking
    # `-V` of every tool rather than four (#347) makes it 26 cases on a
    # Linux host and 22 on macOS, scaled here to 6.2 s. It is a rate from
    # one mix of cases applied to another: the nine new ones are all `-V`,
    # which reads every attached board, so expect it to run low on a bench
    # with several boards and high on a macOS host, which has four fewer
    # cases. A target's own runs replace it as soon as three of them have
    # measured it (learned.py), which is what the countdown then uses.
    "Host CLI surface": _default(Estimate(6.2)),
    "Ubertooth count": _default(Estimate(0.5)),
    "Ubertooth identity": _default(Estimate(0.4)),
    "Debug register read": _default(Estimate(0.5)),
    "Ubertooth util compositions": _profiles(Estimate(0.2), Estimate(2.6)),
    "Argument validation": _profiles(Estimate(15.8), Estimate(92.2)),
    "Radio setting transitions": _profiles(Estimate(7.2), Estimate(28.2)),
    "BLE CRC transitions": _profiles(Estimate(26.3), Estimate(153.5)),
    "BLE settings": _profiles(Estimate(1.1), Estimate(8.7)),
    "Debug registers": _profiles(Estimate(0.6), Estimate(5.5)),
    "Ubertooth util modes": _profiles(Estimate(13.0), Estimate(44.3)),
    "Multi-board selection": _profiles(Estimate(1.2), Estimate(4.4)),
    "BLE mode conflicts": _default(Estimate(4.2)),
    "Ubertooth util conflicts": _default(Estimate(15.9)),
    # One full-band sweep of its duration, plus the board reads around it.
    "Spectrum RSSI sanity": _default(Estimate(1.0, per_duration=1)),
    "Spectrum analyzer": _profiles(
        Estimate(0.6, per_duration=1), Estimate(7.1, per_duration=10)
    ),
    "Classic Bluetooth receive": _profiles(
        Estimate(2.7, per_duration=3), Estimate(12.5, per_duration=12)
    ),
    "Raw receive": _profiles(
        Estimate(2.1, per_duration=3), Estimate(9.0, per_duration=20)
    ),
    # A reset at each end, a killed capture, one capture of the duration, and
    # about a dozen board reads around them. Measured on the Linux lab host at
    # fork 166b4f91 with a 3 s duration.
    "Board state between runs": _default(Estimate(15.5, per_duration=1)),
    # The earlier estimates (0.9 and 3.9) plus the measured difference the
    # capture-destination cases make, taken in two steps on the macOS host at
    # fork 7cbc191a. First: smoke 2.9-3.1 s against 1.1 s without them, and
    # comprehensive 6.1-6.7 s against 3.0 s, so +1.9 and +3.3. Then three
    # more cases: comprehensive 8.0 s of command time against 6.2 s for the
    # same run without them, so +1.8 more, and smoke unchanged, those three
    # being comprehensive-only. 0.9 + 1.9 = 2.8 and 3.9 + 3.3 + 1.8 = 9.0.
    # Smoke then gained the /proc case, which a macOS run leaves out and so
    # cannot be measured here; the three commands of a case like it cost
    # about 0.3 s on the hosts above, hence 2.8 + 0.3 = 3.1. A Linux
    # measurement should replace that estimate rather than this reasoning.
    # Then the record-fields case, five commands at that same 0.1 s each, in
    # both profiles, and one more command on the comprehensive filtered-PCAP
    # case: 3.1 + 0.5 = 3.6 and 9.0 + 0.6 = 9.6, reasoned the same way
    # because the host it would be measured on was carrying other work.
    # 15 cases and 65 commands in smoke, 33 and 143 comprehensive (14 and 60,
    # and 32 and 138, where there is no /proc and the one case about it is not
    # applicable); the smoke figure carries that case, so a macOS run comes
    # in a little under it. Each pair of command-time figures was taken back
    # to back, because a busy host takes two to six times longer, which is
    # why an estimate is a countdown and never a timeout.
    #
    # Harness #363 added two cases, both in smoke and so in both profiles:
    # the payload types read out of a capture and -u 00. Their own commands,
    # timed back to back on the idle Linux bench host against a build of fork
    # 2026-11-DEV2 over three passes of five rounds, cost 0.318 s and 0.128 s
    # a round, against 0.130 s for a case of the same shape already here, so
    # 0.45 s of command time; a macOS run of the same test measured 0.5 s
    # more in smoke and 0.6 s more comprehensive. Those are the same two
    # figures the record-fields case above measured, and the two cases are
    # separate work, so both increases stand: 3.1 + 0.5 + 0.5 = 4.1 and
    # 9.0 + 0.6 + 0.6 = 10.2.
    "Raw receive replay": _profiles(Estimate(4.1), Estimate(10.2)),
    # Two replays of a handful of records in smoke, four in comprehensive,
    # each with the fixture writer and the directory listing beside it.
    # The cases' own commands, timed back to back on the macOS host against
    # a build of fork 4b28aec9: 0.89-0.95 s in smoke and 1.58-1.66 s
    # comprehensive, most of it starting the processes rather than replaying
    # eight records.
    #
    # Harness #363 added the levels case, in smoke and so in both profiles.
    # Its own commands cost 0.123 s a round on the idle Linux bench host
    # against a build of fork 2026-11-DEV2, over three passes of five rounds,
    # against 0.079 s for a case of the same shape already here; 0.2 s is
    # that plus the harness's own per-case cost on the macOS host, where the
    # four comprehensive cases came to 0.6 s between them.
    "BLE replay": _profiles(Estimate(1.1), Estimate(1.8)),
    # Two cases and 35 commands in smoke, three and 52 comprehensive.
    # Measured on the Linux bench host: smoke 11.2 s, and comprehensive 17.5,
    # 18.3 and 23.4 s over three runs. Each case establishes idle, reads three
    # settings, runs a capture for its 1.5 s settling time plus the SIGINT
    # teardown, reads the file it was left with, reads the board's channel and
    # squelch, asks for its identity, and restores; the comprehensive-only
    # sweep case costs the most, since specan takes longest to reach its
    # floor.
    "Interrupting a capture": _profiles(Estimate(11.2), Estimate(18.5)),
    # Every case is a handful of short commands, so the cost goes with the
    # command count: the btle replay and dfu read-back cases (#333) take the
    # commands this test runs on a macOS host from 192 to 212 in smoke and
    # from 256 to 322 comprehensive, and the figures are the earlier ones
    # scaled by that. Measuring the two profiles end to end on this host gave
    # 10.7 s and 16.6 s, and the same comprehensive suite without the new
    # cases took 89.3 s on a busy host, which is why the count and not a
    # stopwatch is what the increase rests on.
    "Refused command lines": _profiles(Estimate(11.0), Estimate(26.0)),
    # No hardware: 6 cases and 39 commands in smoke, 16 and 112
    # comprehensive, all of them short (preflight is not counted, as it is in
    # no estimate). The lightest run of them on the macOS host cost 0.135 s a
    # command; the same runs with that host busy took two to six times
    # longer, which is why an estimate is a countdown, never a timeout.
    "Failed capture files": _profiles(Estimate(5.3), Estimate(15.1)),
    "Util mode refusals": _profiles(Estimate(1.9), Estimate(3.0)),
    "DFU image checks": _profiles(Estimate(8.7), Estimate(37.4)),
    "Python tools": _profiles(Estimate(0.5), Estimate(7.7)),
    "BLE extcap interfaces": _profiles(Estimate(0.4), Estimate(1.4)),
    "BLE extcap capture": _profiles(
        Estimate(1.3, per_duration=1), Estimate(10.2, per_duration=6)
    ),
    # ESTIMATE_PENDING_HW: the DK check and one capture, then three.
    "BLE extcap in tshark": _profiles(
        Estimate(25.0, per_duration=1), Estimate(35.0, per_duration=3)
    ),
    # Three load runs of four flashes each took 10.4-10.7 s.
    "Flash reliability": _default(Estimate(0, per_count=("flashes", 2.65))),
    "DFU images": _profiles(Estimate(11.7), Estimate(69.0)),
    # Three runs on the Linux lab host (Acroname USBHub3+, three other boards
    # powered off, 5 s capture) took 12.5-16.7 s, median 15.2 s: about 1.3 s
    # to a settled enumeration and switch-off, 1.5 s to confirm the board is
    # alone, the queries and capture, and under 0.1 s to restore.
    "Exclusive Ubertooth topology": _default(Estimate(10.0, per_duration=1)),
    # Nine passing runs on the Linux lab host at fork 699960b (one guard
    # board) took 11.5-14.9 s, median 13.3 s: about 6 s to -n (two resets),
    # the result within about 1 s, then the reads and restores of both
    # boards and the guard checks. Before fork #601 a run failed in about
    # 18 s (no result by the 10 s deadline, fork #26).
    "Two-device range test": _default(Estimate(13.5)),
    # ESTIMATE_PENDING_HW
    "Capture known BLE advertiser": _profiles(
        Estimate(25.0, per_duration=5), Estimate(70.0, per_duration=16)
    ),
    # ESTIMATE_PENDING_HW
    # A capture case takes its capture up to three times
    # (connection_cases.CAPTURE_ATTEMPTS), so the duration counts three
    # times per capture case: one in smoke, three comprehensive. A run that
    # catches the connection first time takes a third of this.
    "Capture known BLE connection": _profiles(
        Estimate(30.0, per_duration=4), Estimate(60.0, per_duration=10)
    ),
    # ESTIMATE_PENDING_HW
    # One capture case, taken up to CAPTURE_ATTEMPTS times, plus the pair's
    # setup and the reads after the capture.
    "Follow a BLE connection across its end": _default(
        Estimate(35.0, per_duration=3)
    ),
    # Each capture case opens both ESP32 bridges, which resets both boards,
    # and waits for the link: about 13 s before its capture starts, plus the
    # board reads around it. Two capture cases in smoke, four comprehensive.
    "Capture known Classic device": _profiles(
        Estimate(40.0, per_duration=2), Estimate(80.0, per_duration=4)
    ),
    "Follow Classic Bluetooth piconet": _profiles(
        Estimate(40.0, per_duration=2), Estimate(80.0, per_duration=4)
    ),
    # ESTIMATE_PENDING_HW: two capture cases in smoke, four comprehensive,
    # each with the same bring-up as the two tests above and a record check
    # per capture file afterwards.
    "Classic capture record fields": _profiles(
        Estimate(45.0, per_duration=2), Estimate(95.0, per_duration=4)
    ),
    # Per flash-and-session cycle, measured on the lab bench (the first
    # figures at fork b2cd6c68, the work #332 added at 80123099): the option
    # values, two firmware writes of about 4 s, the version reads, the peer
    # reset, and a console session whose radio checks end as soon as their
    # answer arrives. The sessions themselves were 27 s and 40 s for smoke
    # and 89 s and 95 s for comprehensive, over two runs; what varies between
    # them is how long the inquiry and each page train take, 2.1 to 6.4 s for
    # the inquiry and 0.3 to 17.4 s for a page, so there is room above the
    # measurement here rather than a figure a slow page would overrun. The session also transmits the chosen packet types on
    # the connection, pages the peer again, and runs the two scan modes.
    # Comprehensive adds five more transmit runs, another page, and the page
    # nothing answers, which costs the firmware's whole 45 s page attempt.
    "Classic btbr console": _profiles(
        Estimate(0, per_count=("sessions", 80.0)),
        Estimate(0, per_count=("sessions", 140.0)),
    ),
    # ESTIMATE_PENDING_HW
    # Each case brings the pair up, sets the channel map, captures for the
    # duration, and judges the recovery: about 20 s around a capture, and the
    # capture itself may run twice, because whether a channel was heard
    # depends on where the sweep and the hop sequence met (afh_cases.py,
    # attempts). Two capture cases in smoke, three comprehensive, so the
    # duration counts four times and six.
    "Recover AFH map": _profiles(
        Estimate(40.0, per_duration=4), Estimate(60.0, per_duration=6)
    ),
    # Measured on the bench at `duration: 25s`: the whole test took 112 s in
    # one run and 155 s in another, the difference being how many of other
    # people's devices answered the inquiry and had to be paged for a name.
    # The estimate carries the slower of the two, so 105 s besides the
    # durations. That is the pair's own
    # bring-up and two adapter reads a case, plus what the tool spends
    # outside the window `-t` names -- for `-s` a whole BlueZ inquiry, since
    # the alarm is armed only after it, and a name read per device either
    # half found. The measured commands ran 78 s (`-s`) and 61 s against
    # windows of 111 s and 100 s: the tool ends its own sweep, so a healthy
    # run finishes well inside the deadlines it is allowed. The same two
    # cases in both profiles.
    "Bluetooth inquiry scan": _profiles(
        Estimate(105.0, per_duration=2), Estimate(105.0, per_duration=2)
    ),
    # Unlike the scan, this tool bounds nothing itself: the driver ends the
    # command, so a case costs its whole window whatever the page really
    # took. The window is the duration plus FOLLOW_SETUP_S (52 s), and 3.4 s
    # more for -a, so the fixed part is the allowance plus the pair's
    # bring-up. Measured at `duration: 25s`: 189 s for the two smoke cases,
    # so 139 s besides the durations, with the commands themselves running
    # 83 s and 87 s. The third case, `-q`, costs what the first does.
    "Follow Classic piconet over HCI": _profiles(
        Estimate(140.0, per_duration=2), Estimate(210.0, per_duration=3)
    ),
    # Measured on the bench: 49 s for the whole invocation, of which the case
    # itself was 36 s -- the write, the cycle, the recovery write and the
    # revision read that confirms it, and the cleanup's checks -- and the
    # rest the guard baseline
    # and the check after it, on a host with six other boards. Rounded up for
    # a bench with more guards to read, or a slower hub.
    invalidapp_cases.TEST_NAME: _default(Estimate(70.0)),
    # ESTIMATE_PENDING_HW: the capture runs for the whole duration the plan
    # asks for, and around it are the advertiser check (which may flash the
    # DK), the board and watchdog reads at either end, and the cleanup. The
    # fixed part is the sum of those bounded commands rather than a
    # measurement; the duration is what a soak's time really is, and it is
    # hours, so the figure around it hardly moves the countdown.
    advertiser_soak_cases.TEST_NAME: _default(Estimate(60.0, per_duration=1)),
    # ESTIMATE_PENDING_HW: as the address soak. The capture runs for the whole
    # duration the plan asks for; around it are the fixture checks (two DKs
    # identified and put in their roles), the board and watchdog reads at
    # either end, and the cleanup. The fixed part is the sum of those bounded
    # commands rather than a measurement, and the duration is what a soak's
    # time really is. The checkpoints are not in the fixed part, because they
    # scale with the duration rather than sitting outside it: each one costs
    # seconds inside a window measured in hours, so it falls well inside the
    # step's own allowance (follow_soak_cases.`step_timeout`) and would be
    # noise in a countdown.
    follow_soak_cases.TEST_NAME: _default(Estimate(90.0, per_duration=1)),
})


# Tests whose cases need an ordinary link: they listen for ambient traffic or
# hold a live connection with a peer, so an attenuated board would cost them
# the very packets they check for. A plan may still override this per role.
RF_DEFAULTS: Mapping[str, Mapping[str, PlacementRequirement]] = MappingProxyType({
    name: MappingProxyType({"Ubertooth": NORMAL_LINK})
    for name in (
        "Classic Bluetooth receive",
        "Classic capture record fields",
        "Raw receive",
        "BLE extcap capture",
        "BLE extcap in tshark",
        "Capture known BLE advertiser",
        "Capture known BLE connection",
        "Classic btbr console",
        "Follow a BLE connection across its end",
        advertiser_soak_cases.TEST_NAME,
        follow_soak_cases.TEST_NAME,
        # The Classic peer tests: each one captures a piconet the ESP32 pair
        # holds, and wants an ordinary link for the same reason as the rest.
        # On a bench whose default board is the attenuated one they would
        # otherwise select it silently. This list is still not every test
        # that takes a capture (`Interrupting a capture` and the soaks carry
        # no default), so it is a list of the tests that declare the need,
        # not a rule derived from what a test does.
        "Capture known Classic device",
        "Follow Classic Bluetooth piconet",
        "Recover AFH map",
        # The two that add a host Bluetooth adapter to the same pair.
        "Bluetooth inquiry scan",
        "Follow Classic piconet over HCI",
    )
})


def builtin_catalog() -> TestCatalog:
    """Return the fixed catalog; YAML cannot add executable implementations."""
    def stateful_test(name, description, builder, *, coverage=True, extra=None,
                      **options):
        arguments = {"hosts": HOSTS, "Ubertooth": UBERTOOTH}
        if coverage:
            arguments["coverage"] = COVERAGE
        arguments.update(extra or {})
        return TestDefinition(
            name,
            description,
            MappingProxyType(arguments),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=builder,
            guards_other_ubertooths=True,
            **options,
        )

    definitions = [
        TestDefinition(
            "Host version",
            "Host tools version line",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
            ("ubertooth-util", "-V"),
            timeout_s=HOST_VERSION_TIMEOUT_S,
            output_pattern=_HOST_VERSION_PATTERN,
            expected_returncodes=HOST_VERSION_RETURNCODES,
        ),
        # `Host CLI surface` asks this of every tool, so this entry adds no
        # coverage. It is kept, where the five unimplemented identity
        # entries were removed (#347), because it is the cheapest step a
        # plan can hold: one command, no board, no pattern, and it answers
        # "did this build produce a runnable tool" in half a second. The
        # five were removed for having no implementation and no plan to be
        # the cheap version of anything.
        TestDefinition(
            "Help smoke",
            "ubertooth-util help executes",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
            ("ubertooth-util", "-h"),
        ),
        TestDefinition(
            "Host CLI surface",
            "Help and host version for every C host tool the fork builds",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
            command_builder=_host_cli_surface_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Ubertooth count",
            "ubertooth-util -N matches the registered Ubertooths on the host",
            MappingProxyType({"hosts": HOSTS, "Ubertooth": UBERTOOTH}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=_ubertooth_count_cases,
            guards_other_ubertooths=True,
        ),
        # Which bootloader is under the application firmware. Preflight reads
        # it on every run whose commit has the read at all, which is what no
        # catalog case can do: a case cannot tell whether the commit under
        # test has `ubertooth-util -B`, and against one that has not it would
        # fail on a board that is perfectly set up (bootloader.py).
        #
        # It is the one identity read left as a catalog name with no cases.
        # `Firmware version`, `Firmware compile info`, `Device serial`,
        # `Part ID` and `Board ID` were names here too, and every one of
        # them is a case of `Ubertooth identity` with the same command and
        # the same check, so a plan could never have wanted the standalone
        # entry; they were removed rather than reimplemented
        # (#347). Preflight still reports its per-board checks under the
        # first three of those names, and "Mandatory preflight" in
        # docs/tests.md is where those reads are written down.
        _device_test(
            "Bootloader identity",
            "Which bootloader is installed under the application firmware",
        ),
        TestDefinition(
            "Ubertooth identity",
            "Isolated and composed firmware and hardware identity readback",
            MappingProxyType({"hosts": HOSTS, "Ubertooth": UBERTOOTH}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=_ubertooth_identity_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Debug register read",
            "Read a CC2400 register",
            MappingProxyType({
                "hosts": HOSTS,
                "Ubertooth": UBERTOOTH,
                "register": ArgumentRule(_register, default=0),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=_debug_register_cases,
        ),
        stateful_test(
            "Ubertooth util compositions",
            "Read-only ubertooth-util option compositions and compatible pairs",
            transitions.util_composition_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "Argument validation",
            "Arguments each tool refuses leave the device unchanged",
            transitions.argument_validation_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "Radio setting transitions",
            "Channel, PA level, squelch, and LED set/stop/reset/restore transitions",
            transitions.radio_setting_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "BLE CRC transitions",
            "BLE CRC verification applied by sniffing modes, then restored",
            transitions.ble_crc_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "BLE settings",
            "ubertooth-btle access address, follow target, and error allowance without a peer",
            tool_cases.btle_settings_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "Debug registers",
            "ubertooth-debug register lists, ranges, verbosity, and exit status",
            tool_cases.debug_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "Ubertooth util modes",
            "ubertooth-util -I identify and -q LED spectrum run, stop on -S, and "
            "restore; -m with no range-test result",
            util_cases.util_mode_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "Multi-board selection",
            "-U all, no-selector listing, and -U<index> with two or more boards",
            util_cases.multi_board_cases,
            continue_on_case_failure=True,
        ),
        stateful_test(
            "BLE mode conflicts",
            "ubertooth-btle rejects more than one major mode",
            transitions.btle_conflict_cases,
            coverage=False,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Ubertooth util conflicts",
            "Conflicting continuous ubertooth-util modes are rejected",
            MappingProxyType({
                "hosts": HOSTS,
                "Ubertooth": UBERTOOTH,
                # SqueakyFloors/ubertooth#82 is fixed (#134): conflicts are
                # refused before any mode starts. The guard stays: a regression
                # that accepted a pair would start range test, repeater, or TX
                # test and transmit.
                "allow RF transmission": ArgumentRule(_required_true, required=True),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=transitions.util_conflict_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Two-device range test",
            "ubertooth-util -e (repeater) on the responder and -n (range test) "
            "on the sender: the sender's -m reports a clean round trip at or "
            "below the plan's PA level, and both boards stop and are restored",
            MappingProxyType({
                "host": ArgumentRule(_required_text, required=True),
                "sender": ArgumentRule(_selector, required=True),
                "responder": ArgumentRule(_selector, required=True),
                # Both boards transmit, the sender at every PA level up to 0 dBm.
                "allow RF transmission": ArgumentRule(_required_true, required=True),
                "maximum PA level": ArgumentRule(_pa_level, default=7),
            }),
            MappingProxyType({"sender": "Ubertooth", "responder": "Ubertooth"}),
            command_builder=util_cases.range_test_cases,
            guards_other_ubertooths=True,
        ),
        # The two transmit-side weak-link entries (#168). Both need a
        # transmitter driven at a known PA level and a receiver behind a known
        # attenuation, which the range test has not got: it reports only the
        # level that got through, not a figure per level. Neither has an
        # implementation, because the figure each would judge has not been
        # measured yet; weaklink.CHECKS names the measurement.
        TestDefinition(
            "Transmit power steps",
            "Nothing yet: the sender holds a continuous transmit at each of "
            "the plan's PA levels while the attenuated receiver sweeps the "
            "channel it transmits on, so the RSSI step per level is a figure "
            "taken at a known attenuation",
            MappingProxyType({
                "host": ArgumentRule(_required_text, required=True),
                "sender": ArgumentRule(_selector, required=True),
                "receiver": ArgumentRule(_selector, required=True),
                # The sender transmits at every level the plan lists.
                "allow RF transmission": ArgumentRule(_required_true, required=True),
                "PA levels": ArgumentRule(_pa_levels, required=True),
                # The channel both boards hold: the sender transmits on it and
                # the receiver sweeps around it, so a reading per level needs
                # it fixed and named rather than left to the boot default.
                "channel": ArgumentRule(_channel_mhz, required=True),
                # The whole test, as everywhere else, not per level: the
                # catalog's own estimate reads this argument that way.
                "duration": DURATION,
            }),
            MappingProxyType({"sender": "Ubertooth", "receiver": "Ubertooth"}),
            # Two boards transmitting and receiving on a named channel, so the
            # host's other boards are read before and after, as the range test
            # does.
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Receiver sensitivity",
            "Nothing yet: a sender held at one known PA level while the "
            "attenuated receiver reports what it hears, so a figure belongs "
            "to a known level rather than to whatever ambient traffic the "
            "bench had; which reading the receiver takes is not settled",
            MappingProxyType({
                "host": ArgumentRule(_required_text, required=True),
                "sender": ArgumentRule(_selector, required=True),
                "receiver": ArgumentRule(_selector, required=True),
                "allow RF transmission": ArgumentRule(_required_true, required=True),
                "PA level": ArgumentRule(_pa_level, required=True),
                "channel": ArgumentRule(_channel_mhz, required=True),
                "duration": DURATION,
            }),
            MappingProxyType({"sender": "Ubertooth", "receiver": "Ubertooth"}),
            guards_other_ubertooths=True,
        ),
        _device_test("BLE monitor", "Monitor BLE traffic", extra={"duration": DURATION}),
        TestDefinition(
            "Spectrum analyzer",
            "ubertooth-specan sweeps stay in the requested range and cover it",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE,
                "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=tool_cases.spectrum_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Spectrum RSSI sanity",
            "ubertooth-specan reports live RSSI: a noise floor at every frequency, "
            "a negative mean, and varying readings",
            MappingProxyType({"hosts": HOSTS, "Ubertooth": UBERTOOTH, "duration": DURATION}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=tool_cases.rssi_sanity_cases,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Classic Bluetooth receive",
            "ubertooth-rx and ubertooth-afh bounded receive runs without a peer: "
            "timeouts, options, and capture files",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE,
                "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=receive_cases.classic_receive_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Raw receive",
            "ubertooth-dump symbol streams and ubertooth-ego receive without a peer",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE,
                "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=receive_cases.raw_receive_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            boardstate_cases.TEST_NAME,
            "A capture killed with SIGKILL leaves the board in its mode; the "
            "next run on the same board stops it, captures only its own "
            "records, and keeps the killed capture as evidence",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=boardstate_cases.board_state_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            interrupt_cases.TEST_NAME,
            "A real SIGINT to a running capture: one Ctrl-C ends the run "
            "promptly with exit 0, leaves no process behind and nothing still "
            "streaming, leaves a capture file a reader can open, and leaves a "
            "board back at its idle settings that answers the next command at "
            "once",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=interrupt_cases.interrupt_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Raw receive replay",
            "ubertooth-rx -i decodes a synthetic raw capture with no Ubertooth: "
            "LAP/UAP lines, filters, PCAPNG/PCAP/dump output, bad input files, "
            "the destinations that mean standard output, and the destination "
            "pairs that are one file",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=receive_cases.replay_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            btle_replay_cases.TEST_NAME,
            "ubertooth-btle -R replays a synthetic low-energy dump with no "
            "Ubertooth: a promiscuous capture's connection parameters and the "
            "CONNECT_IND beside the packets on that access address, the "
            "company identifiers libbtbb names and the one it does not, the "
            "-d records written back, and the refusal of a replay with no "
            "major mode",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=btle_replay_cases.btle_replay_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Refused command lines",
            "A refused command line creates, truncates, and leaves behind no "
            "file, for every file option of btle, follow, rx, dump, specan, and "
            "dfu, and scan and follow refuse a stray word, with no Ubertooth "
            "(UBERTOOTH_TEST_NO_DEVICES=1)",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=refusal_cases.refusal_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Failed capture files",
            "A capture that fails leaves no misleading file: a run stopped at "
            "the board lookup creates none, a replay that reads no whole record "
            "takes back the ones it created, a destination it cannot create is "
            "named with its reason, and a capture that did decode packets is "
            "kept as a file a reader can open, with no Ubertooth "
            "(UBERTOOTH_TEST_NO_DEVICES=1 and ubertooth-rx -i)",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=capture_failure_cases.capture_failure_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "Util mode refusals",
            "ubertooth-util refuses -i with any other mode, -z with -i, and "
            "the removed -f, before any board lookup, with no Ubertooth "
            "(UBERTOOTH_TEST_NO_DEVICES=1)",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=util_refusal_cases.util_refusal_cases,
            continue_on_case_failure=True,
        ),
        TestDefinition(
            "DFU image checks",
            "ubertooth-dfu -d refuses bad images before any board lookup, and -s "
            "suffixes binaries and refuses .dfu and suffixed inputs, with no "
            "Ubertooth (UBERTOOTH_TEST_NO_DEVICES=1)",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=dfu_host_cases.dfu_host_cases,
            continue_on_case_failure=True,
            # One -s case compares against the run's own test image; without
            # one it is left out as not applicable and the rest still run.
            build_needs=Needs(firmware_build=True),
        ),
        TestDefinition(
            "Python tools",
            "ubertooth-specan-ui -h without a display and btle-extcap DLT and "
            "config listings, with no Ubertooth (needs --build --with-python "
            "for specan-ui)",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=python_cases.python_tool_cases,
            continue_on_case_failure=True,
            # ubertooth-specan-ui exists only in the Python tools build, and
            # its cases are left out without one; a test whose every case ran
            # a Python tool would declare build_required instead.
            build_needs=Needs(python_tools=True),
        ),
        TestDefinition(
            "BLE extcap interfaces",
            "btle-extcap --extcap-interfaces lists every attached Ubertooth",
            MappingProxyType({"hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE}),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=python_cases.extcap_interface_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "BLE extcap capture",
            "btle-extcap --capture into a FIFO read as Wireshark would, stopped "
            "with SIGTERM: a DLT 256 PCAP stream, --channel reaches the board",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH, "coverage": COVERAGE,
                "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=python_cases.extcap_capture_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "BLE extcap in tshark",
            "real tshark installs btle-extcap, captures through it from a "
            "registered nRF52840 DK advertiser, and its own dissector decodes "
            "the DK's address: with a capture filter it cannot apply, and "
            "stopped with SIGINT partway",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "advertiser": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({"Ubertooth": "Ubertooth", "advertiser": "nRF52840 DK"}),
            command_builder=tshark_cases.extcap_tshark_cases,
            # A host without tshark runs none of it, and --dry-run says so.
            needs=tshark_cases.NEEDS,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Its first case flashes the DK when the board runs another
            # firmware version, so the run holds the host lock.
            writes_firmware=True,
        ),
        TestDefinition(
            "Capture known BLE advertiser",
            "ubertooth-btle -n and -f capture a registered nRF52840 DK advertiser "
            "on the channel it advertises on, with its payload, in PCAP and "
            "PCAPNG, and none of its packets on another channel",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "advertiser": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
                "interval": ArgumentRule(_advertising_interval, default=100),
                "payload": ArgumentRule(_payload_tag, default="c0ffee"),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth", "advertiser": "nRF52840 DK"}),
            command_builder=advertiser_cases.known_advertiser_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Its first case flashes the DK when the board runs another
            # firmware version, so the run holds the host lock.
            writes_firmware=True,
        ),
        TestDefinition(
            "Capture known BLE connection",
            "ubertooth-btle -f follows a connection between two registered "
            "nRF52840 DKs and the capture holds the CONNECT_IND parameters the "
            "central asked for, with packets longer than a link without Data "
            "Length Extension allows",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "central": ArgumentRule(_selector, default="default"),
                "peripheral": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
                "interval": ArgumentRule(_connection_interval, default=30),
                "update": ArgumentRule(_connection_interval, default=60),
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth", "central": "nRF52840 DK",
                "peripheral": "nRF52840 DK",
            }),
            check_arguments=_check_connection_duration,
            command_builder=connection_cases.known_connection_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Its first case flashes a DK when the board runs another
            # firmware version, so the run holds the host lock.
            writes_firmware=True,
        ),
        TestDefinition(
            connection_cases.FOLLOW_END_TEST_NAME,
            "ubertooth-btle -f follows two registered nRF52840 DKs across "
            "several of the central's reconnections: the capture holds a "
            "CONNECT_IND for each connection it carries packets for, and the "
            "board is idle and answering as soon as the capture stops",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "central": ArgumentRule(_selector, default="default"),
                "peripheral": ArgumentRule(_selector, default="default"),
                "duration": DURATION,
                "interval": ArgumentRule(_connection_interval, default=30),
                "update": ArgumentRule(_connection_interval, default=60),
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth", "central": "nRF52840 DK",
                "peripheral": "nRF52840 DK",
            }),
            check_arguments=_check_follow_end_duration,
            command_builder=connection_cases.follow_end_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Its first case flashes a DK when the board runs another
            # firmware version, so the run holds the host lock.
            writes_firmware=True,
        ),
        TestDefinition(
            advertiser_soak_cases.TEST_NAME,
            "A long ubertooth-btle -n capture of a registered nRF52840 DK "
            "advertising its address on a schedule, read twice from the one "
            "capture -- the documented AdvA text line and the capture's own "
            "records, CRC checked -- and judged on whether both readers met "
            "the detection objective, agreed about which windows the address "
            "was present in, and found no gap, unexplained process death, or "
            "unrecovered board",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "advertiser": ArgumentRule(_selector, default="default"),
                # No `coverage:`: one capture either way, and what the run
                # costs and decides is in the figures below.
                "duration": DURATION,
                "channel": ArgumentRule(_advertising_channel, default=37),
                "interval": ArgumentRule(_advertising_interval, default=100),
                "payload": ArgumentRule(_payload_tag, default="c0ffee"),
                # The advertiser's schedule: every on period is a detection
                # with a deadline, every off period a silence with a known
                # cause.
                "advertiser on": ArgumentRule(_duration, default="30s"),
                "advertiser off": ArgumentRule(_duration, default="30s"),
                # How often the run writes a progress record, which is what
                # a long run is watched and reviewed through.
                "sample every": ArgumentRule(_duration, default="30s"),
                # Presence: how wide a window is, and how many in a row
                # count as sustained rather than a first sighting.
                "presence window": ArgumentRule(_duration, default="2s"),
                "presence windows": ArgumentRule(_positive_integer, default=5),
                # A capture with no packet at all for this long is restarted.
                "liveness timeout": ArgumentRule(_duration, default="60s"),
                # How soon after an on period starts the advertiser must be
                # heard, and in what share of the periods.
                "detect within": ArgumentRule(_duration, default="10s"),
                "minimum detection share": ArgumentRule(_share, default=0.9),
                # The longest silence allowed inside an on period, and the
                # floor on the rate over the minutes the advertiser was on.
                # A gap is measured inside one period, so the limit has to
                # sit between `detect within` and `advertiser on`
                # (_check_address_soak).
                "maximum gap": ArgumentRule(_duration, default="15s"),
                "minimum packets per minute": ArgumentRule(
                    _nonnegative_integer, default=10),
                # How far the two readers may differ: a share of the windows
                # either of them saw the address in, because the readers
                # differ for reasons that scale with the run (docs/tests.md).
                "maximum window disagreement share": ArgumentRule(_share, default=0.02),
                # Restarts the run may make and still pass. A restart is
                # recorded whatever this says; this is what makes one a
                # failure.
                "maximum restarts": ArgumentRule(_nonnegative_integer, default=0),
                # Optional, like `max_watchdog_resets`: without it the
                # capture process's memory is reported and nothing fails on
                # it, since what counts as growth over three days is not
                # established.
                "maximum memory growth": ArgumentRule(_positive_integer),
                # Kill the capture once, at the middle of the run, with no
                # stop sent to the board, so the run carries evidence that
                # the restart path recovers.
                "restart probe": ArgumentRule(_boolean, default=False),
                # How large one capture file may grow, in MiB, before the run
                # starts another at the next off period. The capture records
                # every advertiser on the channel, so this is what keeps a
                # run of days out of the host's disk.
                "capture file limit": ArgumentRule(_positive_integer, default=256),
                # And how much of the captures the whole run keeps, in MiB.
                # Past it the oldest complete capture and transcript are
                # removed, newest kept: every figure is counted as the
                # records arrive, so what a run loses is the oldest bytes to
                # read by hand rather than its result.
                "capture budget": ArgumentRule(_positive_integer, default=4096),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth", "advertiser": "nRF52840 DK"}),
            check_arguments=_check_address_soak,
            command_builder=advertiser_soak_cases.address_soak_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Its first case flashes the DK when the board runs another
            # firmware version, so the run holds the host lock.
            writes_firmware=True,
        ),
        TestDefinition(
            follow_soak_cases.TEST_NAME,
            "A long ubertooth-btle -f capture across a changing link: two "
            "registered nRF52840 DKs cycle through the peripheral advertising, "
            "a connection the central makes and carries, and both going off the "
            "air, and the run is judged on whether the board found the "
            "advertiser, acquired the connection from its CONNECT_IND and "
            "recorded its packets, stopped reporting both when the fixture went "
            "away, and reacquired it each cycle -- with the board's watchdog "
            "record read at planned checkpoints, which stop the capture cleanly "
            "rather than reading it beside one",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "central": ArgumentRule(_selector, default="default"),
                "peripheral": ArgumentRule(_selector, default="default"),
                # No `coverage:`: one capture either way, and what the run
                # costs and decides is in the figures below.
                "duration": DURATION,
                "channel": ArgumentRule(_advertising_channel, default=37),
                # The cycle: the peripheral advertising and waiting to be
                # connected, the link up and being written to, and the pair
                # off the air altogether.
                "advertising": ArgumentRule(_duration, default="20s"),
                "connected": ArgumentRule(_duration, default="30s"),
                "absent": ArgumentRule(_duration, default="20s"),
                # How often the capture is stopped cleanly so the board's
                # watchdog record can be read, which is never done beside a
                # running capture.
                "checkpoint every": ArgumentRule(_duration, default="30m"),
                # How often the run writes a progress record, which is what
                # a long run is watched and reviewed through.
                "sample every": ArgumentRule(_duration, default="1m"),
                # What the fixture puts on the air: the connection interval
                # the central asks for, and what it writes while the link is
                # up. Not its advertising interval: the peripheral role has
                # no command for one, and advertises at the firmware's own
                # 100 to 150 ms (followsoak.PERIPHERAL_ADV_INTERVAL_MAX_MS).
                "connection interval": ArgumentRule(
                    _connection_interval, default=30),
                "write bytes": ArgumentRule(_write_bytes, default=244),
                "write period": ArgumentRule(_positive_integer, default=50),
                # Presence: how wide a window is, and how many in a row count
                # as sustained rather than a first sighting.
                "presence window": ArgumentRule(_duration, default="2s"),
                "presence windows": ArgumentRule(_positive_integer, default=5),
                # A capture with no packet at all for this long is restarted.
                # It has to clear a connected phase, an absent phase and the
                # next detection, because the fixture is deliberately off the
                # air for part of every cycle (_check_follow_soak).
                "liveness timeout": ArgumentRule(_duration, default="2m"),
                # How soon after an advertising phase starts the peripheral
                # must be heard, and in what share of the phases.
                "detect within": ArgumentRule(_duration, default="10s"),
                "minimum detection share": ArgumentRule(_share, default=0.9),
                # How long the central has to report the link it was told to
                # make -- a phase it misses is a fixture failure, not a
                # follow one -- and how soon after the phase started a
                # CONNECT_IND for the pair must be in the capture.
                "connect within": ArgumentRule(_duration, default="10s"),
                "acquire within": ArgumentRule(_duration, default="15s"),
                "minimum follow share": ArgumentRule(_share, default=0.8),
                # Packets of the connection one phase must hold. A sniffer
                # catches a fraction of a followed connection, so this is a
                # floor a working follow clears rather than an expected count.
                "minimum connection packets": ArgumentRule(
                    _positive_integer, default=3),
                # How long into an absent phase the fixture may still be
                # heard: the packets already on the air, and already in the
                # tool's buffers, arrive after the stop.
                "quiet after": ArgumentRule(_duration, default="3s"),
                # The longest silence allowed inside an advertising phase. A
                # gap is measured inside one phase, so the limit has to sit
                # between `detect within` and `advertising`.
                "maximum gap": ArgumentRule(_duration, default="15s"),
                # Restarts the run may make and still pass. A restart is
                # recorded whatever this says; this is what makes one a
                # failure. A checkpoint is not a restart.
                "maximum restarts": ArgumentRule(_nonnegative_integer, default=0),
                # Optional, like `max_watchdog_resets`: without it the
                # capture process's memory is reported and nothing fails on
                # it, since what counts as growth over three days is not
                # established.
                "maximum memory growth": ArgumentRule(_positive_integer),
                # How large one capture file may grow, in MiB, before the run
                # starts another at the next absent phase, and how much of
                # the captures the whole run keeps. Past the budget the
                # oldest complete capture and transcript are removed, newest
                # kept.
                "capture file limit": ArgumentRule(_positive_integer, default=256),
                "capture budget": ArgumentRule(_positive_integer, default=4096),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth",
                              "central": "nRF52840 DK",
                              "peripheral": "nRF52840 DK"}),
            check_arguments=_check_follow_soak,
            command_builder=follow_soak_cases.follow_soak_cases,
            # Two things this test deliberately does not do, both so it can
            # run beside other work on the host for its whole window: it
            # guards no other Ubertooth, because every command it runs names
            # its board by serial; and it writes no firmware, so run-suite
            # takes no host lock (leases.needs_host_lock). Its first case
            # checks the DKs' firmware and refuses rather than flashing.
        ),
        TestDefinition(
            "Capture known Classic device",
            "ubertooth-rx finds two registered ESP32 Classic peers streaming "
            "over a Serial Port Profile link: the survey lists the piconet "
            "central's LAP, an unfiltered run reports its packets, and swapping "
            "the peers' roles moves the LAP on the air to the other board",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=classic_cases.known_classic_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Follow Classic Bluetooth piconet",
            "ubertooth-rx -l and -u follow the piconet two registered ESP32 "
            "Classic peers make and report its packets, and report nothing for "
            "the LAP of the peer that is not the central",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=classic_cases.follow_classic_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Classic capture record fields",
            "Decode the BR/EDR records ubertooth-rx writes while two "
            "registered ESP32 Classic peers hold a link: every record carries "
            "this piconet's LAP and reference address, and its own payload "
            "length, HEC, and CRC agree with the header beside them",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=classic_cases.record_fields_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Classic btbr console",
            "ubertooth-btbr on the separate btbr firmware image: an option "
            "value outside the radio's field is refused before a board is "
            "looked for, the board reports that image by name, the prompt "
            "answers its refusals, an inquiry hears a registered ESP32 "
            "Classic peer by address and by name, a page connects to it and "
            "completes the LMP exchange, the chosen packet types go out on "
            "that connection, the page completes again, the two scan modes "
            "run and return, and the cleanup always writes the application "
            "image back",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "acceptor": ArgumentRule(_selector, default="default"),
                # Flash-and-session cycles, for a long suite; one by
                # default, because each costs two firmware writes. Bounded,
                # for the same reason: a plan asking for a hundred would
                # write the board's flash two hundred times.
                "sessions": ArgumentRule(_btbr_sessions, default=1),
                # The board transmits: an inquiry train, a page train, and
                # then a live connection it holds as the piconet central.
                "allow RF transmission": ArgumentRule(_required_true,
                                                      required=True),
                "coverage": COVERAGE,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth", "acceptor": ESP32_CLASSIC_PEER,
            }),
            command_builder=btbr_cases.btbr_console_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Every case writes the board's firmware and ends with the
            # application image written back and read, so the test must be
            # the last one on its board.
            establishes_firmware=True,
            writes_firmware=True,
            # Every case names both firmware images and runs a Python tool
            # (`ubertooth-btbr` is installed by a --with-python build), so a
            # run that carries neither is refused rather than leaving the
            # case out: a board flashed with the alternate image needs the
            # application image to put back. Both images are named, because a
            # run can carry the application one without the alternate (#279).
            build_required=Needs(firmware_build=True, python_tools=True,
                                 btbr_build=True),
        ),
        TestDefinition(
            "Recover AFH map",
            "ubertooth-afh recovers the Adaptive Frequency Hopping map of a "
            "piconet two registered ESP32 Classic peers are held to: a "
            "40-channel map and the 20-channel minimum, each compared with the "
            "map the leader's controller reports the link hopping over at the "
            "start, the middle, and the end of the capture",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=afh_cases.afh_map_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Bluetooth inquiry scan",
            "ubertooth-scan drives a host Bluetooth adapter and an Ubertooth "
            "at once: the adapter's inquiry reports both registered ESP32 "
            "Classic peers by address and one of them by name, and the "
            "Ubertooth's sweep reports the piconet the pair holds with the "
            "channel map it built for it",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                BLUETOOTH_ADAPTER: ArgumentRule(_selector, default="default"),
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                BLUETOOTH_ADAPTER: BLUETOOTH_ADAPTER,
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=bluez_cases.inquiry_scan_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
        ),
        TestDefinition(
            "Follow Classic piconet over HCI",
            "ubertooth-follow pages the piconet's central from a host "
            "Bluetooth adapter and follows it on the clock it read over HCI; "
            "with -a it asks that link for its channel map, which this peer "
            "runs with Adaptive Frequency Hopping off, so the tool says so "
            "and follows without it",
            MappingProxyType({
                "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                BLUETOOTH_ADAPTER: ArgumentRule(_selector, default="default"),
                "acceptor": ArgumentRule(_selector, default="default"),
                "initiator": ArgumentRule(_selector, default="default"),
                "coverage": COVERAGE, "duration": DURATION,
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                BLUETOOTH_ADAPTER: BLUETOOTH_ADAPTER,
                "acceptor": ESP32_CLASSIC_PEER, "initiator": ESP32_CLASSIC_PEER,
            }),
            command_builder=bluez_cases.follow_over_hci_cases,
            continue_on_case_failure=True,
            guards_other_ubertooths=True,
            # Every case pages the target, so the whole test needs the
            # privilege an HCI connection takes: run-suite records it blocked
            # with the real reason rather than letting three cases fail as if
            # the peers had stopped (#401).
            needs=Needs(hci_connect=True),
        ),
        _device_test(
            "BLE soak",
            "Long BLE monitor",
            extra={"duration": DURATION, "sample every": SAMPLE_EVERY},
        ),
        _device_test(
            "Spectrum soak",
            "Long spectrum capture",
            extra={"duration": DURATION, "sample every": SAMPLE_EVERY},
        ),
        _device_test(
            "Classic Bluetooth receive soak",
            "Long BR/EDR receive",
            extra={"duration": DURATION, "sample every": SAMPLE_EVERY},
        ),
        TestDefinition(
            "Flash reliability",
            "Repeated exact-verified firmware writes",
            MappingProxyType({
                "hosts": HOSTS,
                "Ubertooth": UBERTOOTH,
                "platforms": ArgumentRule(_platforms, required=True),
                "flashes": ArgumentRule(_positive_integer, required=True),
                "image sequence": ArgumentRule(_sequence, required=True),
                "KGV sha256": ArgumentRule(_sha256, required=True),
                "KGV revision": ArgumentRule(_required_text, required=True),
                # Rotate flashes across every registered Ubertooth on the host
                # (selected first): A, B, A, B, ...
                "alternate Ubertooths": ArgumentRule(_boolean, default=False),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=_flash_reliability_cases,
            establishes_firmware=True,
            writes_firmware=True,
            check_arguments=_check_alternation,
            # Which images it flashes is the plan's to say, and a run without
            # one of them is refused: the run's build makes "test build", and
            # the known-good image beside it is a published release, which
            # only --artifact can pass.
            image_labels_argument="image sequence",
        ),
        stateful_test(
            "DFU images",
            "ubertooth-dfu -s suffixing, -u read-back of the written image, and "
            "refusal of bad images before any write",
            dfu_cases.dfu_cases,
            continue_on_case_failure=True,
            # Every case ends with the test image written, read back, and
            # verified, so the test must be the last on its board.
            establishes_firmware=True,
            writes_firmware=True,
            # Every case writes the run's own test image, and a run without
            # one is refused rather than leaving them out (#257).
            build_required=Needs(firmware_build=True),
        ),
        TestDefinition(
            "Exclusive Ubertooth topology",
            "Only the selected board is powered; no-selector tools reach it",
            MappingProxyType({
                "hosts": HOSTS,
                "platforms": ArgumentRule(_platforms, required=True),
                "Ubertooth": UBERTOOTH,
                "exclusive Ubertooth": ArgumentRule(_required_true, required=True),
                "capture duration": ArgumentRule(_duration, default="5s"),
            }),
            MappingProxyType({"Ubertooth": "Ubertooth"}),
            command_builder=_exclusive_topology_cases,
        ),
        TestDefinition(
            "MAC polling soak",
            "Long-running MAC bucket and query workflow",
            MappingProxyType({
                "hosts": HOSTS,
                "platforms": ArgumentRule(_platforms, required=True),
                "Ubertooth": UBERTOOTH,
                "advertiser": ArgumentRule(_selector, required=True),
                "exclusive Ubertooth": ArgumentRule(_required_true, required=True),
                "duration": DURATION,
                "query every": ArgumentRule(_duration, required=True),
                "log every": ArgumentRule(_duration, required=True),
                "bucket count": ArgumentRule(_positive_integer, required=True),
                "bucket rotation": ArgumentRule(_duration, required=True),
                "listener silence timeout": ArgumentRule(_duration, required=True),
                "empty buckets restart after": ArgumentRule(_duration, required=True),
                "restart suppression": ArgumentRule(_duration, required=True),
                "restart timeout extension": ArgumentRule(_duration, required=True),
                "post-restart wait": ArgumentRule(_duration, required=True),
                "search timeout": ArgumentRule(_duration, required=True),
                "maximum consecutive misses": ArgumentRule(
                    _nonnegative_integer, required=True
                ),
                "maximum restarts": ArgumentRule(
                    _nonnegative_integer, required=True
                ),
            }),
            MappingProxyType({
                "Ubertooth": "Ubertooth",
                "advertiser": "nRF52840 DK",
            }),
        ),
    ]
    definitions.extend(
        [
            _device_test(
                "Capture eero setup beacon", "Trigger and capture an eero setup beacon",
                extra={"eero": ArgumentRule(_required_text, required=True)},
            ),
            TestDefinition(
                "Recover a BLE connection promiscuously",
                "ubertooth-btle -p recovers a live connection between two "
                "nRF52840 DKs that hops with channel selection algorithm 1",
                MappingProxyType({
                    "hosts": HOSTS, "Ubertooth": UBERTOOTH,
                    "central": ArgumentRule(_selector, default="default"),
                    "peripheral": ArgumentRule(_selector, default="default"),
                    "coverage": COVERAGE, "duration": DURATION,
                }),
                MappingProxyType({
                    "Ubertooth": "Ubertooth",
                    "central": "nRF52840 DK",
                    "peripheral": "nRF52840 DK",
                }),
            ),
            stateful_test(
                invalidapp_cases.TEST_NAME,
                "An image the bootloader must refuse to start, then a power "
                "cycle: the board has to come up as a DFU bootloader device "
                "rather than run the image, and the run's own image goes back "
                "on it",
                invalidapp_cases.invalid_application_cases,
                coverage=False,
                extra={
                    # The hub the board must be on. It is not a bound role:
                    # a role the registry has not got is left out as not
                    # applicable, and a missing hub must block this test
                    # instead (the planner records it, #269). So the alias
                    # is an argument, and plan_power_cycle resolves it
                    # against the board's own `power:` mapping, which is
                    # where the port comes from.
                    "USB power controller": ArgumentRule(_required_text, required=True),
                    # Stated, not inferred, like every other test that
                    # switches power. The reader of the bus and the hub's
                    # driver both differ by platform (the managed hub is
                    # reached through a Linux serial-device path), and a
                    # test that can leave a board needing the bench is
                    # aimed at a bench on purpose.
                    "platforms": ArgumentRule(_platforms, required=True),
                },
                # It leaves an unstartable image in the board's flash until
                # its own recovery step writes the test image back, so it
                # establishes the firmware it verifies and must be the last
                # test on its board.
                establishes_firmware=True,
                writes_firmware=True,
                switches_power=True,
                destructive=True,
                # Every case writes the run's own image, as DFU images does;
                # a run without one is refused rather than leaving it out.
                build_required=Needs(firmware_build=True),
            ),
            _device_test("Receive-only firmware", "Exercise a receive-only build"),
            # No Uberducky or E-GO device test: exercising those fixtures is
            # out of scope (#89). `Host CLI surface` covers ubertooth-ducky
            # and ubertooth-ego help, `Argument validation` their refusals,
            # and `Raw receive` runs `ubertooth-ego -r` with nothing in range.
        ]
    )
    return TestCatalog(
        _accept_required_hardware(_accept_placement_arguments(
            _accept_watchdog_limit(_accept_platforms(
            replace(
                definition,
                estimates=ESTIMATES.get(definition.name),
                rf_defaults=RF_DEFAULTS.get(definition.name),
            )))))
        for definition in definitions
    )


def _accept_required_hardware(definition: TestDefinition) -> TestDefinition:
    """Accept `required hardware:` on every test that binds hardware.

    A test that binds none has no hardware a registry could be without, so
    it cannot take the argument, as it cannot take `rf:`.
    """
    if (not definition.hardware_roles
            or REQUIRED_HARDWARE_ARGUMENT in definition.arguments):
        return definition
    arguments = dict(definition.arguments)
    arguments[REQUIRED_HARDWARE_ARGUMENT] = REQUIRED_HARDWARE
    return replace(definition, arguments=MappingProxyType(arguments))


def _accept_placement_arguments(definition: TestDefinition) -> TestDefinition:
    """Accept `rf:` and `weak link:` on every test that binds hardware, so a
    plan can ask for a placement for any of its roles. A test that binds none
    cannot."""
    if not definition.hardware_roles:
        return definition
    arguments = dict(definition.arguments)
    arguments.setdefault("rf", RF_ARGUMENT)
    arguments.setdefault("weak link", WEAK_LINK_ARGUMENT)
    return replace(definition, arguments=MappingProxyType(arguments))


def _accept_watchdog_limit(definition: TestDefinition) -> TestDefinition:
    """Accept `max_watchdog_resets:` on every test, so a plan can make the
    firmware's self-reset count a pass/fail measure for any of them."""
    if WATCHDOG_LIMIT_ARGUMENT in definition.arguments:
        return definition
    arguments = dict(definition.arguments)
    arguments[WATCHDOG_LIMIT_ARGUMENT] = MAX_WATCHDOG_RESETS
    return replace(definition, arguments=MappingProxyType(arguments))


def _accept_platforms(definition: TestDefinition) -> TestDefinition:
    """Accept `platforms:` on every test, so a plan can restrict any step.

    Tests that must state their platforms (Flash reliability, Exclusive
    Ubertooth topology, MAC polling soak) declare their own required rule and
    keep it.
    """
    if "platforms" in definition.arguments:
        return definition
    arguments = dict(definition.arguments)
    arguments["platforms"] = PLATFORMS_ARGUMENT
    return replace(definition, arguments=MappingProxyType(arguments))
