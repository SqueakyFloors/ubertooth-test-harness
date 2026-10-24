"""Ubertooth option-composition and stateful transition case builders.

Command contracts below come from the Ubertooth fork sources:

- ``ubertooth-util`` runs reset, stop, configuration, reports, then at most one
  final mode action, regardless of argv order. Squelch set/get run after the
  range-test, repeater, TX-test, and LED-specan actions return, so squelch is
  dropped when combined with one of those (xmas runs after squelch); no
  generated case combines them.
- Set options store a single value, so a repeated set option is
  last-option-wins. ``-c`` (MHz) and ``-C`` (index) share one value.
- Optional option arguments must be attached (``-c2426``); a detached value is
  parsed as a positional argument.
- ``-a0`` is parsed as a PA-level query, so PA level 0 cannot be set. On
  hardware (Oct 2026, current main and 2020-12-R1 firmware) a powered-on or
  reset board reports PA level 7, so a board only reads 0 if something set the
  CC2400 front end that way; cases that would change PA then skip.
- ``-S`` requests idle mode. From idle it changes nothing, so settings persist
  between idle commands. Leaving an active mode (xmas, range test, repeater,
  TX test, BLE sniffing) runs the firmware idle reset: channel 2441 MHz,
  squelch -120, LEDs off. PA level is kept; only RX/TX mode entry writes it.
  ``-r`` reboots to the same boot defaults, including PA level 7.
- ``ubertooth-btle -n``/``-f`` set the advertising channel and LE modulation,
  start the mode, then apply ``-v[01]``; on exit the tool stops the mode, which
  runs the idle reset (including modulation back to basic rate).
  ``ubertooth-btle -v`` reads the CRC setting without starting a mode.
- ``-l`` sets the USR LED; ``-d`` sets USR, RX, and TX together and reports
  all three. No option sets RX or TX alone, so an LED state with RX != TX can
  be read but not written back.
- ``ubertooth-btle -p`` (promiscuous) deliberately disables CRC verification
  inside the firmware, so it is not a CRC-retention mode.
- Stopping a mode also clears the BR/EDR target address and the LE advertising
  channel, and ``-r`` clears the BLE access address. No case records or
  restores these; none is readable through the options the cases use.
- Only range test (``-n``) and repeater (``-e``) write the PA register.
- ``ubertooth-util -i`` (ISP) and ``-f`` (DFU) leave the application firmware;
  they are never used in conflict cases because an accepted conflict could
  strand the board outside the application.
"""

from dataclasses import dataclass, replace
from itertools import combinations, product
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Mapping

from ubertooth_harness import capturefile
from ubertooth_harness.cases import CommandCase, guard_roles
from ubertooth_harness.patterns import (
    BOARD_ID_PATTERN,
    COMPILE_INFO_PATTERN,
    FIRMWARE_PATTERN,
    PART_ID_PATTERN,
    serial_pattern,
)
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    StateCaseError,
    Step,
    Unrestorable,
    compatible_pairs,
    require,
)


CRC_PATTERN = r"CRC: (?P<value>DO NOT verify|verify)"
CRC_VALUES = {"verify": "1", "DO NOT verify": "0"}
MODE_TIMEOUT_S = 4.0
RESET_TIMEOUT_S = 15.0
BOOT_DEFAULT_CHANNEL = "2441"
UNUSED_INDEX = "99"


def _serial(resource_identities: Mapping, test_name: str) -> str:
    try:
        return resource_identities["Ubertooth"]["serial"].lower()
    except KeyError as error:
        raise StateCaseError(f"{test_name} requires a registered serial") from error


def _util(serial: str, *options: str) -> tuple[str, ...]:
    return ("ubertooth-util", "-U", serial) + options


def _btle(serial: str, *options: str) -> tuple[str, ...]:
    return ("ubertooth-btle", "-U", serial) + options


def _stop(serial: str, step_id: str = "stop", name: str = "Stop current operation"):
    return Step(
        step_id,
        name,
        _util(serial, "-S"),
        output_patterns=(r"Stopping ubertooth device",),
    )


def _identity(serial: str) -> Step:
    return Step(
        "restored-device-identity",
        "Restored device identity",
        _util(serial, "-s"),
        output_patterns=(serial_pattern(serial),),
    )


# ---------------------------------------------------------------------------
# Settings that ubertooth-util can read and write


def _pa_restorable(value: str) -> bool:
    return value != "0"


def _pa_reject(value: str) -> str:
    if not _pa_restorable(value):
        return (
            "skipped: original PA level 0 cannot be restored by ubertooth-util "
            "(-a0 is a query)"
        )
    return ""


@dataclass(frozen=True)
class Setting:
    key: str
    query: str
    pattern: str
    set_prefix: str
    candidates: tuple[str, ...]
    after_mode_stop: str | None
    reject_original: Callable[[str], str] | None = None
    restorable: Callable[[str], bool] = lambda _value: True
    binary: bool = False
    setter: Callable[[str], tuple[str, ...]] | None = None
    convert: Callable | None = None

    @property
    def original(self) -> str:
        return f"original {self.key}"

    def set_arg(self, value: str) -> str:
        return f"{self.set_prefix}{value}"

    def set_options(self, value: str) -> tuple[str, ...]:
        if self.setter is not None:
            return self.setter(value)
        return (self.set_arg(value),)

    def inverted(self, context) -> str:
        """For two-value settings: the opposite of the recorded original."""
        return "0" if require(context, self.original) == "1" else "1"

    def pick(self, index: int):
        """The index-th candidate that differs from the recorded original.

        Falls back to the declared candidates when too few differ, so a
        setting with one meaningful target (USR LED on) still runs.
        """
        if self.binary:
            return self.inverted
        if not self.candidates:
            raise StateCaseError(f"{self.key} has no candidate values to set")

        def value(context):
            original = require(context, self.original)
            different = [item for item in self.candidates if item != original]
            choices = different if len(different) > index else self.candidates
            return choices[index % len(choices)]
        return value

    def restorable_original(self, context) -> str:
        value = require(context, self.original)
        if not self.restorable(value):
            raise Unrestorable(
                f"{self.key} {value} cannot be set by ubertooth-util"
            )
        return value

    def reject(self, value: str) -> str:
        if self.reject_original is not None:
            reason = self.reject_original(value)
            if reason:
                return reason
        if not self.restorable(value):
            return (
                f"skipped: original {self.key} {value} cannot be restored by "
                "ubertooth-util; not changing the device"
            )
        return ""

    def read_original(self, serial: str) -> Step:
        """Record the original; skip the case before any change if it cannot
        be written back."""
        return Step(
            f"read-original-{self.key}",
            f"Read original {self.key}",
            _util(serial, self.query),
            observe=Observation(
                self.original,
                self.pattern,
                reject=self.reject,
                convert=self.convert,
            ),
        )

    def verify(self, serial: str, step_id: str, name: str, key: str, expected):
        return Step(
            step_id,
            name,
            _util(serial, self.query),
            observe=Observation(key, self.pattern, expected, convert=self.convert),
        )

    def restore(self, serial: str) -> tuple[Step, Step]:
        return (
            Step(
                f"restore-{self.key}",
                f"Restore original {self.key}",
                lambda context: _util(
                    serial, *self.set_options(self.restorable_original(context))
                ),
                mutates=True,
            ),
            self.verify(
                serial,
                f"verify-restored-{self.key}",
                f"Verify restored {self.key}",
                f"restored {self.key}",
                self.restorable_original,
            ),
        )


CHANNEL = Setting(
    "channel", "-C", r"Current frequency: (?P<value>\d+) MHz", "-c",
    ("2426", "2450", "2470"), BOOT_DEFAULT_CHANNEL,
)
PA_LEVEL = Setting(
    "pa-level", "-a", r"PA Level: (?P<value>\d+)", "-a",
    ("5", "6", "3"), None, _pa_reject, _pa_restorable,
)
SQUELCH = Setting(
    "squelch", "-z", r"Squelch set to (?P<value>-?\d+)", "-z",
    ("-60", "-70", "-80"), "-120",
)
USR_LED = Setting(
    "usr-led", "-l", r"USR LED status: (?P<value>[01])", "-l", ("0", "1"), "0",
    binary=True,
)


def _led_triple(match) -> str:
    return "/".join(match.group("usr", "rx", "tx"))


def _leds_restorable(value: str) -> bool:
    _usr, rx, tx = value.split("/")
    return rx == tx


def _leds_setter(value: str) -> tuple[str, ...]:
    # ubertooth-util applies -d (all LEDs) before -l (USR LED).
    usr, rx, _tx = value.split("/")
    return (f"-d{rx}", f"-l{usr}")


LEDS = Setting(
    "leds",
    "-d",
    r"USR LED status: (?P<usr>[01])\s+RX LED status : (?P<rx>[01])\s+"
    r"TX LED status : (?P<tx>[01])",
    "-d",
    (),
    "0/0/0",
    restorable=_leds_restorable,
    setter=_leds_setter,
    convert=_led_triple,
)
CHANNEL_INDEX = replace(
    CHANNEL,
    key="channel-index",
    set_prefix="-C",
    setter=lambda mhz: (f"-C{int(mhz) - 2402}",),
)
# Measured on hardware: power-on and -r both report PA level 7.
PA_BOOT_DEFAULT = "7"


def pa_off_default(context) -> str:
    """A PA level differing from both the boot default and the original."""
    original = require(context, PA_LEVEL.original)
    return next(v for v in PA_LEVEL.candidates if v not in (original, PA_BOOT_DEFAULT))
SMOKE_SETTINGS = (CHANNEL, PA_LEVEL, SQUELCH)
REPEATABLE_SETTINGS = SMOKE_SETTINGS + (CHANNEL_INDEX, USR_LED)
RESTORED_SETTINGS = (CHANNEL, PA_LEVEL, SQUELCH, LEDS)
MODE_RESET_SETTINGS = (CHANNEL, SQUELCH, LEDS)


def _read_originals(serial: str, settings) -> tuple[Step, ...]:
    return tuple(setting.read_original(serial) for setting in settings)


def _cleanup(serial: str, settings, *, crc: bool = False) -> tuple[Step, ...]:
    steps = [_stop(serial, "cleanup-stop", "Return to idle")]
    if crc:
        steps.extend(_crc_restore(serial))
    for setting in settings:
        steps.extend(setting.restore(serial))
    steps.append(_identity(serial))
    return tuple(steps)


def _setting_case(serial: str, setting: Setting) -> StateCase:
    if setting.binary and setting.after_mode_stop is not None:
        # Always set the value the mode stop must clear, so the check is real.
        on = "1" if setting.after_mode_stop == "0" else "0"

        def target(_context):
            return on
    else:
        target = setting.pick(0)
    if setting.after_mode_stop is None:
        after_stop, outcome = target, "kept"
    else:
        after_stop, outcome = setting.after_mode_stop, f"reset to {setting.after_mode_stop}"
    others = tuple(
        item for item in RESTORED_SETTINGS if item is not setting and item is not PA_LEVEL
    )
    return StateCase(
        f"{setting.key}-mode-stop-restore",
        f"{setting.key} across a mode stop, then restored",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            setting.read_original(serial),
        )
        + _read_originals(serial, others)
        + (
            Step(
                "set",
                f"Set different {setting.key}",
                lambda context: _util(serial, *setting.set_options(target(context))),
                mutates=True,
            ),
            setting.verify(
                serial, "verify-set", f"Verify {setting.key} after set",
                f"{setting.key} after set", target,
            ),
            _stop(serial, "idle-stop", "Stop while idle"),
            setting.verify(
                serial, "verify-after-idle-stop",
                f"Verify {setting.key} kept by a stop from idle",
                f"{setting.key} after idle stop", target,
            ),
            Step(
                "all-leds-on",
                "Turn every LED on before the mode",
                _util(serial, "-d1"),
                mutates=True,
            ),
            Step(
                "enter-mode",
                "Enter xmas mode",
                _util(serial, "-x"),
                mutates=True,
            ),
            _stop(serial, "mode-stop", "Stop the active mode"),
            setting.verify(
                serial, "verify-after-mode-stop",
                f"Verify {setting.key} after the mode stops",
                f"{setting.key} after mode stop", after_stop,
            ),
            LEDS.verify(
                serial, "verify-leds-after-mode-stop",
                "Verify every LED is off after the mode stops",
                "leds after mode stop", LEDS.after_mode_stop,
            ),
        ),
        _cleanup(serial, (setting,) + others),
        expected_state=(
            f"{setting.key} kept between idle commands and {outcome} when an "
            "active mode stops; original restored"
        ),
    )


def _repeated_case(serial: str, setting: Setting) -> StateCase:
    last = setting.pick(0)
    if setting.binary:
        def first(context):
            return require(context, setting.original)
    else:
        first = setting.pick(1)
    return StateCase(
        f"{setting.key}-repeated-set-last-wins",
        f"Repeated {setting.set_prefix} is last-option-wins",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            setting.read_original(serial),
            Step(
                "set-twice",
                f"Set {setting.key} twice in one command",
                lambda context: _util(
                    serial,
                    *setting.set_options(first(context)),
                    *setting.set_options(last(context)),
                ),
                mutates=True,
            ),
            setting.verify(
                serial, "verify-last", "Verify the last option won",
                f"{setting.key} after repeated set", last,
            ),
        ),
        _cleanup(serial, (setting,)),
        expected_state=f"last repeated {setting.set_prefix} applied, then restored",
    )


def _index(mhz: str) -> str:
    return f"-C{int(mhz) - 2402}"


def _mixed_channel_case(serial: str, index_last: bool) -> StateCase:
    last = CHANNEL.pick(0)
    first = CHANNEL.pick(1)
    if index_last:
        case_id, name = "channel-mhz-then-index", "-c then -C: index wins"

        def argv(context):
            return _util(serial, f"-c{first(context)}", _index(last(context)))
    else:
        case_id, name = "channel-index-then-mhz", "-C then -c: MHz wins"

        def argv(context):
            return _util(serial, _index(first(context)), f"-c{last(context)}")
    return StateCase(
        case_id,
        name,
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            CHANNEL.read_original(serial),
            Step("set-mixed", name, argv, mutates=True),
            CHANNEL.verify(
                serial, "verify-last", "Verify the last channel option won",
                "channel after mixed set", last,
            ),
        ),
        _cleanup(serial, (CHANNEL,)),
        expected_state="-c and -C share one value; the last one applies",
    )


def _reset_case(serial: str) -> StateCase:
    target = CHANNEL.pick(0)
    reads = (_stop(serial, "establish-idle", "Establish idle before reading"),)
    reads += _read_originals(serial, MODE_RESET_SETTINGS + (PA_LEVEL,))
    return StateCase(
        "full-reset-restores-boot-defaults",
        "Full reset returns to boot defaults and the same device",
        reads
        + (
            _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
            Step(
                "set-non-defaults",
                "Set channel, PA level, squelch, and every LED away from boot defaults",
                lambda context: _util(
                    serial, CHANNEL.set_arg(target(context)),
                    f"-a{pa_off_default(context)}", "-z-60", "-d1",
                ),
                mutates=True,
            ),
            PA_LEVEL.verify(
                serial, "verify-pa-level-before-reset", "PA level changed before reset",
                "pa-level before reset", pa_off_default,
            ),
            CHANNEL.verify(
                serial, "verify-channel-before-reset", "Channel changed before reset",
                "channel before reset", target,
            ),
            SQUELCH.verify(
                serial, "verify-squelch-before-reset", "Squelch changed before reset",
                "squelch before reset", "-60",
            ),
            LEDS.verify(
                serial, "verify-leds-before-reset", "LEDs on before reset",
                "leds before reset", "1/1/1",
            ),
            Step(
                "reset",
                "Full reset",
                _util(serial, "-r"),
                timeout_s=RESET_TIMEOUT_S,
                output_patterns=(r"Resetting ubertooth device",),
                mutates=True,
            ),
            Step(
                "verify-firmware-after-reset",
                "Firmware answers after reset",
                _util(serial, "-v"),
                output_patterns=(FIRMWARE_PATTERN,),
            ),
        )
        + tuple(
            setting.verify(
                serial,
                f"verify-{setting.key}-after-reset",
                f"{setting.key} is the boot default",
                f"{setting.key} after reset",
                setting.after_mode_stop,
            )
            for setting in MODE_RESET_SETTINGS
        )
        + (
            PA_LEVEL.verify(
                serial, "verify-pa-level-after-reset", "pa-level is the boot default",
                "pa-level after reset", PA_BOOT_DEFAULT,
            ),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS + (PA_LEVEL,), crc=True),
        expected_state=(
            "channel, PA level, squelch, and LEDs at boot defaults after reset; every "
            "recorded setting restored on the same serial"
        ),
    )


def _invalid_pa_case(serial: str) -> StateCase:
    return StateCase(
        "pa-level-invalid-set-not-masked",
        "Rejected PA level is not masked by a later report",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            PA_LEVEL.read_original(serial),
            Step(
                "invalid-set-and-report",
                "Invalid -a9 followed by -v",
                _util(serial, "-a9", "-v"),
                expected_returncodes=(1,),
                output_patterns=(FIRMWARE_PATTERN,),
                mutates=True,
            ),
            PA_LEVEL.verify(
                serial, "verify-unchanged", "PA level unchanged",
                "pa-level after invalid set",
                lambda context: require(context, PA_LEVEL.original),
            ),
        ),
        _cleanup(serial, (PA_LEVEL,)),
        expected_state="nonzero exit although the later report succeeded",
    )


def _repeated_all_leds_case(serial: str) -> StateCase:
    def other(context):
        return "1" if require(context, LEDS.original) == "0/0/0" else "0"

    def first(context):
        return "0" if other(context) == "1" else "1"

    return StateCase(
        "all-leds-repeated-set-last-wins",
        "Repeated -d is last-option-wins",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            LEDS.read_original(serial),
            Step(
                "set-twice",
                "Set every LED twice in one command",
                lambda context: _util(serial, f"-d{first(context)}", f"-d{other(context)}"),
                mutates=True,
            ),
            LEDS.verify(
                serial, "verify-last", "Verify the last option won",
                "leds after repeated set",
                lambda context: "/".join([other(context)] * 3),
            ),
        ),
        _cleanup(serial, (LEDS,)),
        expected_state="last repeated -d applied to all LEDs, then each LED restored",
    )


def radio_setting_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Radio setting transitions")
    cases = [_setting_case(serial, setting) for setting in SMOKE_SETTINGS]
    if arguments["coverage"] == "comprehensive":
        cases.append(_setting_case(serial, USR_LED))
        cases.extend(_repeated_case(serial, setting) for setting in REPEATABLE_SETTINGS)
        cases.append(_repeated_all_leds_case(serial))
        cases.append(_mixed_channel_case(serial, index_last=True))
        cases.append(_mixed_channel_case(serial, index_last=False))
        cases.append(_invalid_pa_case(serial))
        cases.append(_reset_case(serial))
    return tuple(cases)


# ---------------------------------------------------------------------------
# Read-only same-command compositions

_QUERY_OPTIONS = (
    ("-v", "firmware", FIRMWARE_PATTERN),
    ("-V", "compile", COMPILE_INFO_PATTERN),
    ("-s", "serial", None),
    ("-p", "part", PART_ID_PATTERN),
    ("-b", "board", BOARD_ID_PATTERN),
    ("-a", "pa-level", r"PA Level: \d+"),
    ("-C", "channel", r"Current frequency: \d+ MHz \(Bluetooth channel -?\d+\)"),
    ("-z", "squelch", r"Squelch set to -?\d+"),
    ("-l", "usr-led", r"USR LED status: [01]"),
    ("-d", "all-leds", r"RX LED status : [01]\s+TX LED status : [01]"),
)
_USR_LED_LINE = r"USR LED status: [01]"


def _query_patterns(options, serial):
    patterns = []
    for option, _label, pattern in _QUERY_OPTIONS:
        if option not in options:
            continue
        patterns.append(serial_pattern(serial) if option == "-s" else pattern)
    if "-l" in options and "-d" in options:
        # -l and -d both print a USR LED line; both reports must be present.
        patterns.append(rf"(?s){_USR_LED_LINE}.*{_USR_LED_LINE}")
    return tuple(patterns)


def _query_case(case_id, name, options, serial):
    return CommandCase(
        case_id,
        name,
        _util(serial, *options),
        _query_patterns(options, serial),
    )


def util_composition_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Ubertooth util compositions")
    every = tuple(option for option, _label, _pattern in _QUERY_OPTIONS)
    cases = [
        _query_case(
            "documented-version-composition",
            "Documented -v -V composition",
            ("-v", "-V"),
            serial,
        ),
        _query_case(
            "full-readback-composition",
            "Every read-only report in one command",
            every,
            serial,
        ),
    ]
    if arguments["coverage"] == "comprehensive":
        covered = {frozenset(case.argv[3:]) for case in cases}
        cases.append(
            CommandCase(
                "repeated-device-selection-last-wins",
                "Repeated -U selects the last device",
                ("ubertooth-util", "-U", UNUSED_INDEX, "-U", serial, "-s"),
                (serial_pattern(serial),),
            )
        )
        labels = {option: label for option, label, _pattern in _QUERY_OPTIONS}
        for first, second in compatible_pairs(every):
            if frozenset((first, second)) in covered:
                continue
            cases.append(
                _query_case(
                    f"pair-{labels[first]}-{labels[second]}",
                    f"Read-only pair {first} {second}",
                    (first, second),
                    serial,
                )
            )
    return tuple(cases)


# ---------------------------------------------------------------------------
# BLE CRC verification across sniffing modes

_CRC_MODES = (("-n", "no-follow"), ("-f", "follow"))
_ADV_INDEXES = ("37", "38", "39")


def _crc_read(serial, step_id, name, key, expected=None):
    return Step(
        step_id,
        name,
        _btle(serial, "-v"),
        observe=Observation(key, CRC_PATTERN, expected, CRC_VALUES),
    )


def _crc_restore(serial):
    return (
        Step(
            "restore-crc",
            "Restore original CRC",
            lambda context: _btle(serial, f"-v{require(context, 'original crc')}"),
            mutates=True,
        ),
        _crc_read(
            serial,
            "verify-restored-crc",
            "Verify restored CRC",
            "restored crc",
            lambda context: require(context, "original crc"),
        ),
    )


def _crc_case(serial, mode, mode_label, target, adv_index):
    opposite = "0" if target == "1" else "1"
    state = "enabled" if target == "1" else "disabled"
    return StateCase(
        f"crc-{state}-during-{mode_label}-adv{adv_index}",
        f"CRC verification {state} during {mode_label} mode on channel {adv_index}",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
        )
        + _read_originals(serial, MODE_RESET_SETTINGS)
        + (
            Step(
                "preset-opposite",
                "Preset opposite CRC before mode",
                _btle(serial, f"-v{opposite}"),
                mutates=True,
            ),
            _crc_read(
                serial,
                "verify-preset",
                "Verify opposite CRC before mode",
                "crc before mode",
                opposite,
            ),
            Step(
                "mode",
                f"Sniff in {mode_label} mode with CRC {state}",
                _btle(serial, mode, f"-A{adv_index}", f"-v{target}"),
                timeout_s=MODE_TIMEOUT_S,
                mutates=True,
                continuous=True,
            ),
            _stop(serial),
            _crc_read(
                serial,
                "verify-effective",
                "Verify CRC applied by the mode",
                "crc after mode",
                target,
            ),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
        expected_state=(
            f"CRC {state} after {mode_label} mode despite opposite preset; "
            "original CRC, channel, squelch, and LEDs restored; the mode stop "
            "returns modulation to basic rate"
        ),
    )


def _repeated_crc_case(serial):
    def other(context):
        return "0" if require(context, "original crc") == "1" else "1"

    return StateCase(
        "crc-repeated-set-last-wins",
        "Repeated ubertooth-btle -v is last-option-wins",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
            Step(
                "set-twice",
                "Set CRC twice in one command",
                lambda context: _btle(
                    serial, f"-v{require(context, 'original crc')}", f"-v{other(context)}"
                ),
                mutates=True,
            ),
            _crc_read(serial, "verify-last", "Verify the last option won",
                      "crc after repeated set", other),
        ),
        _cleanup(serial, (), crc=True),
        expected_state="last repeated -v applied, then original CRC restored",
    )


_CAPTURE_FORMATS = (
    # (option, label, file name, header format, link type)
    ("-r", "pcapng", "capture.pcapng", "pcapng", "256"),
    ("-q", "pcap-le-phdr", "capture.pcap", "pcap", "256"),
    ("-c", "pcap-ppi", "capture-ppi.pcap", "pcap", "192"),
)
# Run the checker by path: it needs only the standard library, so it does not
# depend on the working directory the executor uses.
_CAPTURE_CHECKER = str(Path(capturefile.__file__).resolve())
_CAPTURE_PATTERN = r"Capture format: (?P<value>\w+, link type: \d+)"


def _capture_path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _capture_check(step_id, name, file_name, key, header_format, link_type):
    return Step(
        step_id,
        name,
        lambda context: (
            sys.executable, _CAPTURE_CHECKER, _capture_path(file_name)(context),
        ),
        observe=Observation(
            key, _CAPTURE_PATTERN, f"{header_format}, link type: {link_type}"
        ),
    )


def _capture_absent(step_id, name, file_name):
    return Step(
        step_id,
        name,
        lambda context: (
            sys.executable, _CAPTURE_CHECKER, "--absent", _capture_path(file_name)(context),
        ),
        output_patterns=(r"Capture file absent: ",),
    )


def _capture_reads(serial):
    return (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
    ) + _read_originals(serial, MODE_RESET_SETTINGS)


def _capture_case(serial, mode, mode_label, target, capture):
    option, label, file_name, header_format, link_type = capture
    opposite = "0" if target == "1" else "1"
    state = "enabled" if target == "1" else "disabled"
    return StateCase(
        f"crc-{state}-during-{mode_label}-capture-{label}",
        f"CRC {state} during {mode_label} mode capturing {option} ({label})",
        _capture_reads(serial) + (
            Step(
                "preset-opposite",
                "Preset opposite CRC before mode",
                _btle(serial, f"-v{opposite}"),
                mutates=True,
            ),
            _crc_read(serial, "verify-preset", "Verify opposite CRC before mode",
                      "crc before mode", opposite),
            Step(
                "mode",
                f"Sniff in {mode_label} mode with CRC {state}, capturing {option}",
                lambda context: _btle(
                    serial, mode, "-A37", f"-v{target}",
                    option, _capture_path(file_name)(context),
                ),
                timeout_s=MODE_TIMEOUT_S,
                mutates=True,
                continuous=True,
            ),
            _stop(serial),
            _crc_read(serial, "verify-effective", "Verify CRC applied by the mode",
                      "crc after mode", target),
            _capture_check("verify-capture", f"Verify {option} capture file header",
                           file_name, "capture header", header_format, link_type),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
        expected_state=(
            f"CRC {state} after {mode_label} mode; {option} wrote a {header_format} "
            f"file with link type {link_type}; original CRC, channel, squelch, and "
            "LEDs restored"
        ),
        capture_dir=True,
    )


def _repeated_capture_case(first, second):
    """Capture files are first-option-wins; the extra one is never created.

    -q and -c share one PCAP handle, so either after the other is "extra".
    """
    def case(serial):
        option_a, label_a, file_a, format_a, link_a = first
        option_b, label_b, file_b, _format_b, _link_b = second
        if file_a == file_b:
            file_b = "second-" + file_b
        return StateCase(
            f"capture-repeated-{label_a}-then-{label_b}-first-wins",
            f"Repeated capture {option_a} then {option_b} is first-option-wins",
            _capture_reads(serial) + (
                Step(
                    "mode",
                    f"Sniff with capture {option_a} then {option_b}",
                    lambda context: _btle(
                        serial, "-n", "-A37",
                        option_a, _capture_path(file_a)(context),
                        option_b, _capture_path(file_b)(context),
                    ),
                    timeout_s=MODE_TIMEOUT_S,
                    output_patterns=(r"Ignoring extra capture file: ",),
                    mutates=True,
                    continuous=True,
                ),
                _stop(serial),
                _capture_check("verify-first", f"Verify the {option_a} file was written",
                               file_a, "first capture header", format_a, link_a),
                _capture_absent("verify-second-absent",
                                f"Verify the extra {option_b} file was not created", file_b),
            ),
            _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
            expected_state=(
                f"only the first capture file ({option_a}) written, the extra "
                f"{option_b} ignored; every setting restored"
            ),
            capture_dir=True,
        )
    return case


def _two_handle_capture_case(serial):
    """-r (PCAPNG) and -q (PCAP) use separate handles: both files are written."""
    _, _, file_ng, format_ng, link_ng = _FORMATS["-r"]
    _, _, file_q, format_q, link_q = _FORMATS["-q"]
    return StateCase(
        "capture-pcapng-and-pcap-both-written",
        "Capture -r and -q together writes both files",
        _capture_reads(serial) + (
            Step(
                "mode",
                "Sniff with capture -r and -q",
                lambda context: _btle(
                    serial, "-n", "-A37",
                    "-r", _capture_path(file_ng)(context),
                    "-q", _capture_path(file_q)(context),
                ),
                timeout_s=MODE_TIMEOUT_S,
                forbidden_patterns=(r"Ignoring extra capture file",),
                mutates=True,
                continuous=True,
            ),
            _stop(serial),
            _capture_check("verify-pcapng", "Verify the -r file was written",
                           file_ng, "pcapng header", format_ng, link_ng),
            _capture_check("verify-pcap", "Verify the -q file was written",
                           file_q, "pcap header", format_q, link_q),
        ),
        _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
        expected_state="both capture files written; every setting restored",
        capture_dir=True,
    )


_FORMATS = {item[0]: item for item in _CAPTURE_FORMATS}
_REPEATED_CAPTURES = tuple(
    _repeated_capture_case(_FORMATS[a], _FORMATS[b])
    for a, b in (("-r", "-r"), ("-q", "-c"), ("-c", "-q"))
)


def ble_crc_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "BLE CRC transitions")
    if arguments["coverage"] == "comprehensive":
        combinations = product(_CRC_MODES, ("1", "0"), _ADV_INDEXES)
    else:
        combinations = ((mode, "1", "37") for mode in _CRC_MODES)
    cases = [
        _crc_case(serial, mode, label, target, adv_index)
        for (mode, label), target, adv_index in combinations
    ]
    if arguments["coverage"] == "comprehensive":
        cases.append(_repeated_crc_case(serial))
        cases.extend(
            _capture_case(serial, mode, label, target, capture)
            for (mode, label), target, capture in product(
                _CRC_MODES, ("1", "0"), _CAPTURE_FORMATS
            )
        )
        cases.extend(build(serial) for build in _REPEATED_CAPTURES)
        cases.append(_two_handle_capture_case(serial))
    else:
        # One capture case per format keeps every output format in smoke.
        cases.extend(
            _capture_case(serial, "-n", "no-follow", "1", capture)
            for capture in _CAPTURE_FORMATS
        )
    return tuple(cases)


# ---------------------------------------------------------------------------
# Rejected arguments must leave the device unchanged
#
# Each case is an argument the tool's own parser refuses (checked against the
# fork sources in host/ubertooth-tools/src). The command must exit 1 with the
# tool's error text, start no mode, and leave every readable setting as it
# was. Every command still selects the board by serial, so a regression that
# accepts the argument can only reach the selected board. Commands that would
# transmit if a regression accepted them (btle -s, -i/-I, ducky -q) are not
# used.

_VALIDATED = (CHANNEL, SQUELCH, LEDS)
_PA_READ_KEY = "pa-level before"
_ANY_MODE_START = r"Entering |Starting |Flashing LEDs|systime="


@dataclass(frozen=True)
class Rejection:
    case_id: str
    tool: str
    options: tuple[str, ...]
    error: str  # regex for the tool's own error text
    smoke: bool = False

    def argv(self, serial: str) -> tuple[str, ...]:
        return (self.tool, "-U", serial) + self.options


REJECTIONS = (
    Rejection("util-frequency-below-band", "ubertooth-util", ("-c2401",),
              r"error: frequency must be 2402-2480 MHz", smoke=True),
    Rejection("util-frequency-above-band", "ubertooth-util", ("-c2481",),
              r"error: frequency must be 2402-2480 MHz"),
    Rejection("util-channel-index-79", "ubertooth-util", ("-C79",),
              r"error: channel must be 0-78"),
    Rejection("util-all-with-setter", "ubertooth-util", ("-U", "all", "-a5"),
              r"error: -U all only supports the read-only queries"),
    Rejection("util-all-with-mode", "ubertooth-util", ("-U", "all", "-x"),
              r"error: -U all only supports the read-only queries"),
    Rejection("btle-address-errors-33", "ubertooth-btle", ("-x33",),
              r"Error: can tolerate 0-32 access address bit errors", smoke=True),
    Rejection("btle-adv-index-36", "ubertooth-btle", ("-A36",),
              r"Error: advertising index must be 37, 38, or 39"),
    Rejection("btle-adv-index-40", "ubertooth-btle", ("-A40",),
              r"Error: advertising index must be 37, 38, or 39"),
    Rejection("btle-bad-target-mac", "ubertooth-btle", ("-tnot-a-mac",),
              r"Usage:|usage:"),
    Rejection("ego-length-0", "ubertooth-ego", ("-l0",),
              r"Length must be in \[1,48\]", smoke=True),
    Rejection("ego-length-49", "ubertooth-ego", ("-l49",),
              r"Length must be in \[1,48\]"),
    Rejection("ducky-adv-index-36", "ubertooth-ducky", ("-A36",),
              r"Error: advertising index must be 37, 38, or 39", smoke=True),
    Rejection("ducky-adv-index-40", "ubertooth-ducky", ("-A40",),
              r"Error: advertising index must be 37, 38, or 39"),
    Rejection("ducky-bad-bdaddr", "ubertooth-ducky", ("-anot-an-address",),
              r"Error parsing BD ADDR"),
    Rejection("rx-uap-without-lap", "ubertooth-rx", ("-u", "12"),
              r"Error: UAP but no LAP specified", smoke=True),
    Rejection("rx-survey-with-address", "ubertooth-rx", ("-z", "-l", "123456"),
              r"No address should be specified for survey mode"),
    Rejection("specan-lower-below-band", "ubertooth-specan", ("-l", "2401"),
              r"error: lower frequency must be 2402-2480 MHz", smoke=True),
    Rejection("specan-upper-above-band", "ubertooth-specan", ("-u", "2481"),
              r"error: upper frequency must be 2402-2480 MHz"),
    Rejection("specan-lower-above-upper", "ubertooth-specan", ("-l", "2480", "-u", "2402"),
              r"error: lower frequency must not exceed upper frequency"),
)


def _rejection_case(serial: str, rejection: Rejection) -> StateCase:
    command = " ".join((rejection.tool,) + rejection.options)
    pa_read = Step(
        "read-pa-level",
        "Read PA level",
        _util(serial, PA_LEVEL.query),
        observe=Observation(_PA_READ_KEY, PA_LEVEL.pattern),
    )
    verify_unchanged = tuple(
        setting.verify(
            serial,
            f"verify-unchanged-{setting.key}",
            f"Verify {setting.key} unchanged",
            f"{setting.key} after rejection",
            lambda context, s=setting: require(context, s.original),
        )
        for setting in _VALIDATED
    ) + (
        Step(
            "verify-unchanged-pa-level",
            "Verify PA level unchanged",
            _util(serial, PA_LEVEL.query),
            observe=Observation(
                "pa-level after rejection", PA_LEVEL.pattern,
                lambda context: require(context, _PA_READ_KEY),
            ),
        ),
        _crc_read(serial, "verify-unchanged-crc", "Verify CRC unchanged",
                  "crc after rejection", lambda context: require(context, "original crc")),
    )
    return StateCase(
        f"reject-{rejection.case_id}",
        f"Reject {command}",
        (
            _stop(serial, "establish-idle", "Establish idle before reading"),
            _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
            pa_read,
        )
        + _read_originals(serial, _VALIDATED)
        + (
            Step(
                "reject",
                f"Reject {command}",
                rejection.argv(serial),
                timeout_s=MODE_TIMEOUT_S,
                expected_returncodes=(1,),
                output_patterns=(rejection.error,),
                forbidden_patterns=(_ANY_MODE_START,),
                mutates=True,
            ),
        )
        + verify_unchanged,
        _cleanup(serial, _VALIDATED, crc=True),
        expected_state=(
            "the tool refuses the argument with its own error and exit 1, starts "
            "no mode, and channel, squelch, LEDs, PA level, and CRC are unchanged"
        ),
    )


def argument_validation_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Argument validation")
    selected = (
        REJECTIONS if arguments["coverage"] == "comprehensive"
        else tuple(item for item in REJECTIONS if item.smoke)
    )
    return tuple(_rejection_case(serial, item) for item in selected)


# ---------------------------------------------------------------------------
# Conflicting continuous modes must be rejected before any mode starts

_MODE_OPTIONS = (
    ("-q", "led-specan"),
    ("-n", "range-test"),
    ("-e", "repeater"),
    ("-t", "tx-test"),
    ("-x", "xmas"),
    ("-I", "identify"),
)
_MODE_START = r"Entering |Starting |Flashing LEDs"
_PA_WRITING_MODES = {"-n", "-e"}


def util_conflict_cases(_arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "Ubertooth util conflicts")
    labels = dict(_MODE_OPTIONS)

    def settings_for(first, second):
        # Only range test (-n) and repeater (-e) write the PA register.
        if {first, second} & _PA_WRITING_MODES:
            return RESTORED_SETTINGS
        return MODE_RESET_SETTINGS

    def reads(settings):
        return (
            _stop(serial, "establish-idle", "Establish idle before reading"),
        ) + _read_originals(serial, settings)

    return tuple(
        StateCase(
            f"reject-{labels[first]}-and-{labels[second]}",
            f"Reject {first} {second}",
            reads(settings_for(first, second)) + (
                Step(
                    "reject",
                    f"Reject conflicting {first} {second}",
                    _util(serial, first, second),
                    expected_returncodes=(1,),
                    forbidden_patterns=(_MODE_START,),
                    mutates=True,
                ),
            ),
            _cleanup(serial, settings_for(first, second)),
            expected_state=(
                "usage error before any mode starts; device idle and every "
                "setting, including each LED, restored"
            ),
        )
        # Every mode pair conflicts.
        for first, second in combinations(tuple(labels), 2)
    )


_BTLE_MODES = (("-f", "follow"), ("-n", "no-follow"), ("-p", "promiscuous"))


def btle_conflict_cases(_arguments, _resources, resource_identities):
    """ubertooth-btle must reject more than one major mode before sniffing."""
    serial = _serial(resource_identities, "BLE mode conflicts")
    labels = dict(_BTLE_MODES)
    reads = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        _crc_read(serial, "read-original-crc", "Read original CRC", "original crc"),
    ) + _read_originals(serial, MODE_RESET_SETTINGS)
    options = tuple(labels)
    selections = [tuple(pair) for pair in combinations(options, 2)] + [options]
    return tuple(
        StateCase(
            "reject-" + "-and-".join(labels[item] for item in modes),
            "Reject ubertooth-btle " + " ".join(modes),
            reads + (
                Step(
                    "reject",
                    "Reject " + " ".join(modes),
                    _btle(serial, *modes),
                    timeout_s=MODE_TIMEOUT_S,
                    expected_returncodes=(1,),
                    output_patterns=(r"must choose one -f, -n, or -p",),
                    mutates=True,
                ),
            ),
            _cleanup(serial, MODE_RESET_SETTINGS, crc=True),
            expected_state="usage error before sniffing; every setting restored",
        )
        for modes in selections
    )


# ---------------------------------------------------------------------------
# Guard boards: every other registered Ubertooth on the host must be untouched
#
# Guards are read with queries only (no -S, no set option): firmware revision
# and API, firmware compile info (its build timestamp also catches a
# same-revision reflash), and every setting these tests change. The expected
# value of each check is the guard's own baseline read before the test's
# first case.
#
# ubertooth-util never checks the firmware API, but ubertooth-btle calls
# ubertooth_check_api(), which sends cmd_stop to a board whose firmware API is
# older than the host's libubertooth. The CRC read therefore runs only when
# this check's firmware read showed an API no older than the host tools'.
# No host tool reports the host API, so it is read from UBERTOOTH_API_VERSION
# in the resolved Ubertooth source at the manifest commit.
HOST_API_HEADER = "host/libubertooth/src/ubertooth_interface.h"
_HOST_API_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+UBERTOOTH_API_VERSION[ \t]+0x(?P<value>[0-9a-fA-F]{1,4})\b",
    re.MULTILINE,
)
HOST_API_TIMEOUT_S = 10.0


def read_host_api_header(source) -> str:
    """The API header at the resolved commit (test seam)."""
    result = subprocess.run(
        ["git", "-C", str(source.source_path), "show", f"{source.commit}:{HOST_API_HEADER}"],
        capture_output=True, text=True, timeout=HOST_API_TIMEOUT_S, check=False,
    )
    if result.returncode != 0:
        raise StateCaseError(result.stderr.strip() or f"git show exited {result.returncode}")
    return result.stdout


def host_api_version(source) -> int:
    """UBERTOOTH_API_VERSION of the resolved Ubertooth source; fails closed."""
    where = f"{source.name} {source.commit[:12]}:{HOST_API_HEADER}"
    try:
        text = read_host_api_header(source)
    except (OSError, subprocess.SubprocessError, StateCaseError) as error:
        raise StateCaseError(
            f"cannot read the host API from {where} ({error}); guarded tests refused"
        ) from error
    values = {int(match["value"], 16) for match in _HOST_API_RE.finditer(text)}
    if len(values) != 1:
        raise StateCaseError(
            f"cannot parse one UBERTOOTH_API_VERSION from {where}; guarded tests refused"
        )
    return values.pop()


GUARD_FIRMWARE_KEY = "firmware"
GUARD_FIRMWARE_PATTERN = (
    r"Firmware version:\s+(?P<value>\S+\s+\(API:[0-9a-fA-F]+\.[0-9a-fA-F]+\))"
)
_GUARD_API_RE = re.compile(r"\(API:(?P<major>[0-9a-fA-F]+)\.(?P<minor>[0-9a-fA-F]+)\)")
# Same shape the preflight compile-info check requires.
GUARD_COMPILE_INFO_PATTERN = r"(?P<value>\bubertooth\s+\S+\s+\([^\n]*\S)"
GUARD_READBACK = (
    (GUARD_FIRMWARE_KEY, "-v", GUARD_FIRMWARE_PATTERN, None),
    ("compile-info", "-V", GUARD_COMPILE_INFO_PATTERN, None),
) + tuple(
    (setting.key, setting.query, setting.pattern, setting.convert)
    for setting in (CHANNEL, PA_LEVEL, SQUELCH, LEDS)
)
GUARD_CRC_KEY = "crc"


class _AfterTest:
    """``after`` value for the guard check that follows the whole test."""

    def __str__(self) -> str:
        return "test"

    def __repr__(self) -> str:
        return "AFTER_TEST"


AFTER_TEST = _AfterTest()


def differs_key(key: str) -> str:
    """Context key for a guard read that differed from its baseline."""
    return f"{key} (differs)"


def format_api(version: int) -> str:
    """API version as the host tools print it (%x.%02x)."""
    return f"{(version >> 8) & 0xFF:x}.{version & 0xFF:02x}"


def firmware_api(firmware: str) -> int | None:
    """API version from a recorded guard firmware value, or None."""
    match = _GUARD_API_RE.search(firmware)
    if match is None:
        return None
    return (int(match["major"], 16) << 8) | int(match["minor"], 16)


@dataclass(frozen=True)
class GuardBoard:
    role: str
    serial: str
    # The host tools' API; None (not derived) refuses the btle CRC read.
    host_api: int | None = None

    @property
    def slug(self) -> str:
        return self.role.lower().replace(" ", "-")

    def key(self, setting: str) -> str:
        return f"{self.role} {setting}"


def guard_boards(resource_identities: Mapping) -> tuple[GuardBoard, ...]:
    """The invocation's guard boards, validated as unambiguous -U selectors.

    libubertooth matches -U against the END of a serial, so every selector the
    test uses (selected board and guards) must be a full 32-hex-digit serial
    that no other selector ends with.
    """
    roles = guard_roles(resource_identities)
    if not roles:
        return ()
    boards = tuple(
        GuardBoard(role, str(resource_identities[role].get("serial", "")).lower())
        for role in roles
    )
    selected = str(resource_identities.get("Ubertooth", {}).get("serial", "")).lower()
    selectors = (("Ubertooth", selected),) + tuple((b.role, b.serial) for b in boards)
    for role, selector in selectors:
        if not (len(selector) == 32 and all(c in "0123456789abcdef" for c in selector)):
            raise StateCaseError(
                f"{role} needs a full 32-hex-digit serial when other Ubertooths "
                f"are guarded; registered '{selector}'"
            )
        if selector.isdigit():
            # libubertooth reads an all-digit -U value as a device index.
            raise StateCaseError(
                f"{role} serial '{selector}' is all digits, which libubertooth "
                "reads as a device index; it cannot be guarded by serial"
            )
    if len({selector for _role, selector in selectors}) != len(selectors):
        raise StateCaseError("guard serials must differ from each other and the selected board")
    return boards


def _guard_step(board: GuardBoard, setting, option, pattern, convert, key, expected):
    return Step(
        f"{board.slug}-{setting}",
        f"{board.role} {setting}",
        _util(board.serial, option),
        observe=Observation(key, pattern, expected, convert=convert),
    )


def _btle_api_refusal(board: GuardBoard, firmware_key: str):
    """Refuse the btle CRC read unless this check confirmed a current API.

    ubertooth-btle sends cmd_stop to firmware older than the host API, so an
    unread, unparsable, or older API never reaches ubertooth-btle.
    """
    def refusal(context) -> str:
        if board.host_api is None:
            return (
                f"{board.role}: host tools API not derived; ubertooth-btle not "
                "run, it would stop firmware older than the host tools"
            )
        host = format_api(board.host_api)
        firmware = context.get(firmware_key)
        if firmware is None:
            differs = context.get(differs_key(firmware_key))
            if differs is not None:
                return (
                    f"{board.role} firmware differs from its baseline (now "
                    f"'{differs}'); ubertooth-btle not run on firmware that "
                    "changed"
                )
            return (
                f"{board.role} firmware API was not confirmed in this check; "
                "ubertooth-btle not run, it would stop firmware older than "
                f"the host tools (API {host})"
            )
        api = firmware_api(firmware)
        if api is None or api < board.host_api:
            found = "unreadable" if api is None else format_api(api)
            return (
                f"{board.role} firmware API {found} is older than the host "
                f"tools API {host}, too old to read CRC safely; ubertooth-btle "
                "not run, its API check would stop the guard"
            )
        return ""
    return refusal


def _guard_crc_step(board: GuardBoard, key, expected, firmware_key):
    return Step(
        f"{board.slug}-{GUARD_CRC_KEY}",
        f"{board.role} {GUARD_CRC_KEY}",
        _btle(board.serial, "-v"),
        observe=Observation(key, CRC_PATTERN, expected, CRC_VALUES),
        refuse=_btle_api_refusal(board, firmware_key),
    )


def _baseline(board: GuardBoard, setting: str):
    def expected(context):
        return require(context, board.key(setting))
    return expected


def guard_steps(boards, after=None) -> tuple[Step, ...]:
    """Read-only guard readback.

    With ``after`` unset this records each guard's baseline; otherwise each
    read must equal the baseline (keys are suffixed so each check's observed
    values stay separate in the record). The ubertooth-btle CRC read comes
    last and is refused unless this check's firmware read recorded an API no
    older than the host tools'.
    """
    steps = []
    for board in boards:
        for setting, option, pattern, convert in GUARD_READBACK:
            key = board.key(setting)
            if after is None:
                steps.append(_guard_step(board, setting, option, pattern, convert, key, None))
            else:
                steps.append(_guard_step(
                    board, setting, option, pattern, convert,
                    f"{key} after {after}", _baseline(board, setting),
                ))
        key = board.key(GUARD_CRC_KEY)
        firmware_key = board.key(GUARD_FIRMWARE_KEY)
        if after is None:
            steps.append(_guard_crc_step(board, key, None, firmware_key))
        else:
            steps.append(_guard_crc_step(
                board, f"{key} after {after}", _baseline(board, GUARD_CRC_KEY),
                f"{firmware_key} after {after}",
            ))
    return tuple(steps)
