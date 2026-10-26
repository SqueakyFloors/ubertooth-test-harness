"""Explicit catalog of known tests and their argument/resource contracts."""

from dataclasses import dataclass, replace
import re
from types import MappingProxyType
from typing import Callable, Mapping

from ubertooth_harness.cases import CommandCase, FlashCase, guard_roles
from ubertooth_harness.hostneeds import BLUEZ_TOOLS, NO_NEEDS, Needs, tool_needs
from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    COMPILE_INFO_PATTERN,
    FIRMWARE_PATTERN,
    PART_ID_PATTERN,
    serial_pattern,
)
from ubertooth_harness.stateful import StateCaseError, coverage_profile
from ubertooth_harness import (
    dfu_cases, dfu_host_cases, python_cases, receive_cases, refusal_cases, tool_cases,
    transitions, util_cases, util_refusal_cases,
)


class CatalogError(ValueError):
    """A test name or argument does not match the catalog."""


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
    if any(item not in ("macos", "linux") for item in normalized):
        raise CatalogError("platforms may contain only macos and linux")
    if len(set(normalized)) != len(normalized):
        raise CatalogError("platforms must not contain duplicates")
    return normalized


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


def _coverage(value):
    try:
        return coverage_profile(value)
    except ValueError as error:
        raise CatalogError(str(error)) from error


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
    command_builder: Callable | None = None
    continue_on_case_failure: bool = False
    # The test writes the firmware it then verifies, so preflight does not
    # require the resolved revision on its Ubertooth beforehand.
    establishes_firmware: bool = False
    # Cross-argument rule checked on the normalized arguments (no registry).
    check_arguments: Callable[[Mapping], None] | None = None
    # The planner binds every other registered Ubertooth on the host as a
    # "guard Ubertooth N" resource; the sequencer reads each guard before the
    # test and fails the test if any read changes.
    guards_other_ubertooths: bool = False
    # What every case of the test needs from the host (hostneeds.py). The
    # plan's `platforms:` argument adds a platform need (test_needs).
    needs: Needs = NO_NEEDS
    # Expected wall time by coverage profile (DEFAULT_PROFILE for a test
    # without a coverage argument); see ESTIMATES.
    estimates: Mapping[str, Estimate] | None = None

    def estimated_seconds(self, arguments: Mapping) -> float | None:
        """Expected seconds of one invocation with these normalized arguments.

        A test without calibrated estimates (no implementation yet) is
        expected to run for its duration argument, if it has one. None, never
        an error, when there is no estimate for these arguments (for example
        a per-duration estimate without a duration argument).
        """
        if self.estimates:
            estimate = self.estimates.get(arguments.get("coverage", DEFAULT_PROFILE))
            return None if estimate is None else estimate.seconds(arguments)
        return _duration_argument_seconds(arguments)

    def test_needs(self, arguments: Mapping) -> tuple[Needs, ...]:
        """The test-wide needs: the definition's and the plan's platforms."""
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
        if result.returncode != 0:
            return f"{self.name}: command exited {result.returncode}"
        if self.output_pattern and not re.search(
            self.output_pattern, result.stdout + result.stderr
        ):
            return (
                f"{self.name}: expected output pattern "
                f"{self.output_pattern!r} was not found"
            )
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


# print_version() in libubertooth: "libubertooth <ver> (<rev>), libbtbb <ver> (<rev>)".
_HOST_VERSION_PATTERN = r"libubertooth \S+ \(\S+\), libbtbb \S+ \(\S+\)"


def _host_cli_surface_cases(_arguments, _resources, _resource_identities):
    expected = (
        ("ubertooth-util", 0),
        ("ubertooth-debug", 0),
        ("ubertooth-specan", 0),
        ("ubertooth-rx", 0),
        ("ubertooth-tx", 0),
        ("ubertooth-dump", 0),
        ("ubertooth-btle", 1),
        ("ubertooth-dfu", 1),
        ("ubertooth-ego", 0),
        ("ubertooth-afh", 0),
        ("ubertooth-ducky", 0),
    )
    cases = [
        CommandCase(
            f"{tool}-help",
            f"{tool} help",
            (tool, "-h"),
            (re.escape(tool),),
            expected_returncodes=(returncode,),
        )
        for tool, returncode in expected
    ]
    # Built only with BlueZ (libbluetooth); not applicable where they are not
    # built (every macOS host).
    cases.extend(
        CommandCase(
            f"{tool}-help",
            f"{tool} help (BlueZ builds)",
            (tool, "-h"),
            (re.escape(tool),),
            expected_returncodes=(1,),
            needs=tool_needs(tool),
        )
        for tool in BLUEZ_TOOLS
    )
    # Host-tools version, no device needed: the long -H form and -V on the
    # tools that have it.
    cases.append(
        CommandCase(
            "ubertooth-util-host-version-long",
            "ubertooth-util --host-version",
            ("ubertooth-util", "--host-version"),
            (_HOST_VERSION_PATTERN,),
        )
    )
    cases.extend(
        CommandCase(
            f"{tool}-version",
            f"{tool} -V",
            (tool, "-V"),
            (_HOST_VERSION_PATTERN,),
        )
        for tool in ("ubertooth-rx", "ubertooth-tx", "ubertooth-afh")
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
    compile_info = COMPILE_INFO_PATTERN
    serial_output = serial_pattern(serial)
    part = PART_ID_PATTERN
    board = BOARD_ID_PATTERN
    return (
        CommandCase(
            "firmware-version", "Firmware version", common + ("-v",), (firmware,)
        ),
        CommandCase(
            "firmware-compile-info",
            "Firmware compile info",
            common + ("-V",),
            (compile_info,),
        ),
        CommandCase(
            "device-serial", "Device serial", common + ("-s",), (serial_output,)
        ),
        CommandCase("part-id", "Part ID", common + ("-p",), (part,)),
        CommandCase("board-id", "Board ID", common + ("-b",), (board,)),
        CommandCase(
            "complete-readback-composition",
            "Complete readback composition",
            common + ("-vVspb",),
            (firmware, compile_info, serial_output, part, board),
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
    "multiple Ubertooth devices found!" and indented "  Device N: Serial No:"
    lines to stderr and opens no device, so ubertooth-util and ubertooth-btle
    exit non-zero. These cases only pass when isolation left one board.
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
    "Host CLI surface": _default(Estimate(3.6)),
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
    "Raw receive replay": _profiles(Estimate(0.9), Estimate(3.9)),
    "Refused command lines": _profiles(Estimate(10.0), Estimate(20.7)),
    "Util mode refusals": _profiles(Estimate(1.9), Estimate(3.0)),
    "DFU image checks": _profiles(Estimate(8.7), Estimate(37.4)),
    "Python tools": _profiles(Estimate(0.5), Estimate(7.7)),
    "BLE extcap interfaces": _profiles(Estimate(0.4), Estimate(1.4)),
    "BLE extcap capture": _profiles(
        Estimate(1.3, per_duration=1), Estimate(10.2, per_duration=6)
    ),
    # Three load runs of four flashes each took 10.4-10.7 s.
    "Flash reliability": _default(Estimate(0, per_count=("flashes", 2.65))),
    "DFU images": _profiles(Estimate(11.7), Estimate(69.0)),
    # No run record (Linux only, needs power switching): two queries and the
    # capture, plus up to 5 s removal and 10 s return deadlines with 1 s
    # settles; about 8 s of power switching is assumed.
    "Exclusive Ubertooth topology": _default(Estimate(8.0, per_duration=1)),
})


def builtin_catalog() -> TestCatalog:
    """Return the fixed catalog; YAML cannot add executable implementations."""
    def stateful_test(name, description, builder, *, coverage=True, **options):
        arguments = {"hosts": HOSTS, "Ubertooth": UBERTOOTH}
        if coverage:
            arguments["coverage"] = COVERAGE
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
            "Host tools version and git identity",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
            ("ubertooth-util", "-H"),
            output_pattern=r"libubertooth\s+\S+\s+\(\S+\)",
        ),
        TestDefinition(
            "Help smoke",
            "ubertooth-util help executes",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
            ("ubertooth-util", "-h"),
        ),
        TestDefinition(
            "Host CLI surface",
            "Help and parser availability for every always-built host tool",
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
        _device_test("Firmware version", "Firmware revision"),
        _device_test("Firmware compile info", "Firmware compile identity"),
        _device_test("Device serial", "Microcontroller serial"),
        _device_test("Part ID", "Microcontroller part ID"),
        _device_test("Board ID", "Ubertooth board ID"),
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
            "Raw receive replay",
            "ubertooth-rx -i decodes a synthetic raw capture with no Ubertooth: "
            "LAP/UAP lines, filters, PCAPNG/PCAP/dump output, bad input files",
            MappingProxyType({"hosts": HOSTS, "coverage": COVERAGE}),
            MappingProxyType({}),
            command_builder=receive_cases.replay_cases,
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
            "CLI usage smoke",
            "Exercise host tool usage paths",
            MappingProxyType({"hosts": HOSTS}),
            MappingProxyType({}),
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
            check_arguments=_check_alternation,
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
            TestDefinition(
                "Two-device range test",
                "Two-Ubertooth RF range and integrity test",
                MappingProxyType({
                    "host": ArgumentRule(_required_text, required=True),
                    "sender": ArgumentRule(_selector, required=True),
                    "responder": ArgumentRule(_selector, required=True),
                }),
                MappingProxyType({"sender": "Ubertooth", "responder": "Ubertooth"}),
            ),
            _device_test(
                "Capture known BLE advertiser", "Capture a registered BLE advertiser",
                extra={"advertiser": ArgumentRule(_required_text, required=True)},
            ),
            _device_test(
                "Capture eero setup beacon", "Trigger and capture an eero setup beacon",
                extra={"eero": ArgumentRule(_required_text, required=True)},
            ),
            _device_test(
                "Recover AFH map", "Recover a Classic Bluetooth AFH map",
                extra={"piconet": ArgumentRule(_required_text, required=True)},
            ),
            _device_test(
                "Follow Classic Bluetooth piconet", "Follow a registered piconet",
                extra={"piconet": ArgumentRule(_required_text, required=True)},
            ),
            TestDefinition(
                "Bluetooth inquiry scan", "Run inquiry with a Bluetooth adapter",
                MappingProxyType({
                    "hosts": HOSTS,
                    "Bluetooth adapter": ArgumentRule(_required_text, required=True),
                    "Ubertooth": UBERTOOTH,
                }),
                MappingProxyType({"Ubertooth": "Ubertooth"}),
            ),
            TestDefinition(
                "Reset into invalid application", "Verify recovery from an invalid app",
                MappingProxyType({
                    "hosts": HOSTS,
                    "Ubertooth": UBERTOOTH,
                    "USB power controller": ArgumentRule(_required_text, required=True),
                }),
                MappingProxyType({"Ubertooth": "Ubertooth"}),
            ),
            _device_test("Receive-only firmware", "Exercise a receive-only build"),
            TestDefinition(
                "Uberducky HID", "Exercise an Uberducky HID fixture",
                MappingProxyType({"Uberducky": ArgumentRule(_required_text, required=True)}),
                MappingProxyType({}),
            ),
            TestDefinition(
                "E-GO skateboard capture", "Capture a registered E-GO skateboard",
                MappingProxyType({"skateboard": ArgumentRule(_required_text, required=True)}),
                MappingProxyType({}),
            ),
        ]
    )
    return TestCatalog(
        replace(definition, estimates=ESTIMATES.get(definition.name))
        for definition in definitions
    )
