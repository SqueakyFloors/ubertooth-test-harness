import io
import re
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import capturefile, transitions
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import CatalogError, builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import (
    Observation,
    StateCase,
    StateCaseError,
    Step,
    compatible_pairs,
)
from tests.test_sequencer import CollectSink, run_contract


PROJECT = Path(__file__).resolve().parents[1]
# The fake host tools' API, served as the resolved source's header.
HOST_API = 0x0107
HOST_API_HEADER = (
    "#ifndef __UBERTOOTH_INTERFACE_H\n"
    f"#define UBERTOOTH_API_VERSION 0x{HOST_API:04x}\n"
)


def patch_host_api(header=HOST_API_HEADER):
    """Serve ``header`` as the resolved source's API header for this module."""
    patcher = mock.patch.object(transitions, "read_host_api_header", return_value=header)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def setUpModule():
    patch_host_api()


# ---------------------------------------------------------------------------
# The host tools' getopt and value parsing (fork opt_parse.c), for the fakes.

UTIL_OPTIONS = "U:hnmefiIprsStvbl::a::C::c::d::q::z::9VNx"
BTLE_OPTIONS = "a::r:hfnpU:k::A:s:t:x:c:q:jJiI"
UTIL_VALUES = {
    "l": ("USR LED state", 0, 1, ""),
    "d": ("all-LED state", 0, 1, ""),
    "C": ("channel", 0, 78, ""),
    "c": ("frequency", 2402, 2480, " MHz"),
    "q": ("RSSI threshold", 20, 120, ""),
    "z": ("squelch level", -128, 127, ""),
}
UTIL_MODE_ORDER = ("-q", "-n", "-e", "-t", "-x", "-I", "-i", "-f")


def decimal(text):
    """opt_value_decimal(): digits only."""
    return bool(re.fullmatch(r"[0-9]+", text))


def signed_decimal(text):
    """opt_value_signed_decimal(): digits, optionally after one '-'."""
    return decimal(text[1:] if text.startswith("-") else text)


def not_option(text):
    """opt_value_not_option(): anything but an empty or '-' word."""
    return bool(text) and not text.startswith("-")


def parse_int(text, low, high):
    """opt_parse_int(): a whole decimal in [low, high], else None."""
    if not re.fullmatch(r"-?[0-9]+", text):
        return None
    value = int(text)
    return value if low <= value <= high else None


def getopt(args, optstring, next_value):
    """POSIX getopt as macOS runs it (stops at the first non-option word, and
    after "--"), plus opt_separate_value(): ``next_value(option)`` returns the
    check a following word must pass to become a missing optional value.

    Returns (pairs, leftover words, error or None).
    """
    kinds = {}
    index = 0
    while index < len(optstring):
        option = optstring[index]
        colons = len(optstring[index + 1:]) - len(optstring[index + 1:].lstrip(":"))
        kinds[option] = colons
        index += 1 + colons
    pairs = []
    position = 0
    while position < len(args):
        word = args[position]
        if word == "--":
            position += 1
            break
        if len(word) < 2 or not word.startswith("-"):
            break
        offset = 1
        while offset < len(word):
            option = word[offset]
            kind = kinds.get(option)
            if kind is None:
                return pairs, (), f"invalid option -- {option}\nUsage:"
            if kind == 0:
                pairs.append((option, None))
                offset += 1
                continue
            value = word[offset + 1:] or None
            if value is None and kind == 1:
                position += 1
                if position >= len(args):
                    return pairs, (), f"option requires an argument -- {option}\nUsage:"
                value = args[position]
            elif value is None:
                check = next_value(option)
                if check and position + 1 < len(args) and check(args[position + 1]):
                    position += 1
                    value = args[position]
            pairs.append((option, value))
            break
        position += 1
    return pairs, tuple(args[position:]), None


SERIAL = "abc123"
IDENTITIES = {"Ubertooth": {"serial": SERIAL}}
TRANSITION_TESTS = (
    "Ubertooth util compositions",
    "Radio setting transitions",
    "BLE CRC transitions",
)


class FakeUbertooth:
    """Stateful ubertooth-util/ubertooth-btle model following the fork sources."""

    def __init__(
        self,
        *,
        crc="0",
        lose_mode_crc=False,
        reject_conflicts=True,
        reject_btle_conflicts=True,
        fail=lambda argv: False,
        cancel_on=None,
        serial=SERIAL,
        firmware="git-test",
        api=HOST_API,
        compile_info="ubertooth git-test (builder@host) timestamp",
        next_word=True,
        reject_strays=True,
        pa_zero_sets=True,
        set_and_read=True,
        one_value_each=True,
        version_ends=True,
    ):
        self.serial = serial
        self.version_ends = version_ends
        # Fork behaviors the option cases check; False models older builds:
        # next_word (8b58f75/0b63b01), reject_strays (#146/#155),
        # pa_zero_sets (4ed4a1d; before it -a0 was a query), set_and_read
        # (5b45b3e, #228; before it a later read of a value dropped its set),
        # one_value_each (opt_check_once(); before it an option given a
        # value twice kept the last value, and -c with -C sent the second),
        # and version_ends (f898fe5, #813; before it -V was one report among
        # the others instead of the version command that ends the command).
        self.next_word = next_word
        self.set_and_read = set_and_read
        self.one_value_each = one_value_each
        self.reject_strays = reject_strays
        self.pa_zero_sets = pa_zero_sets
        self.aa = "8e89bed6"
        self.firmware = firmware
        self.api = api
        self.compile_info = compile_info
        # Commands that reached the firmware as cmd_stop (ubertooth_stop()).
        self.stops = 0
        self.state = {
            "channel": "2441", "pa": "7", "squelch": "-90",
            "usr": "1", "rx": "0", "tx": "0", "crc": crc,
        }
        self.lose_mode_crc = lose_mode_crc
        self.reject_conflicts = reject_conflicts
        self.reject_btle_conflicts = reject_btle_conflicts
        self.fail = fail
        self.cancel_on = cancel_on
        self.commands = []
        self.tokens = []
        self.mode = None

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        self.tokens.append(cancellation)
        if self.cancel_on is not None and self.cancel_on(command):
            cancellation.cancel("operator stop")
        if cancellation is not None and cancellation.is_cancelled():
            return CommandResult(command, 130, "", "canceled\n", 0.0, False, True)
        if self.fail(command):
            return self._result(command, 1, "", "injected failure\n", on_output)
        if command[:2] == (sys.executable, transitions._CAPTURE_CHECKER):
            # The real header check, on the files this fake wrote.
            out = io.StringIO()
            with redirect_stdout(out):
                code = capturefile.main(list(command[2:]))
            return self._result(command, code, out.getvalue(), "", on_output)
        selections = [command[i + 1] for i, item in enumerate(command) if item == "-U"]
        assert selections and selections[-1] == self.serial, command
        options = command[3:]
        if command[0] == "ubertooth-btle":
            return self._btle(command, options, on_output)
        if "-U" in options:
            # Two -U name two boards for one command.
            if self.one_value_each:
                return self._result(command, 1, "",
                                    "error: option '-U' given more than once\n",
                                    on_output)
            # Before opt_check_once() the last selection won (getopt overwrote).
            options = options[options.index("-U") + 2:]
        return self._util(command, options, on_output)

    IDLE_RESET = {"channel": "2441", "squelch": "-120", "usr": "0", "rx": "0", "tx": "0"}

    def _stop_mode(self):
        """Firmware cc2400_idle(): only when leaving an active mode."""
        if self.mode is not None:
            self.state.update(self.IDLE_RESET)
            self.mode = None

    def _firmware_line(self):
        return (f"Firmware version: {self.firmware} "
                f"(API:{transitions.format_api(self.api)})")

    def _version_answer(self):
        """The whole -V answer for one board (libubertooth's version file)."""
        return "".join(line + "\n" for line in (
            f"Host tools version: libubertooth 1.1 ({self.firmware}), "
            "libbtbb 1.0 (git-btbb)",
            "libbtbb loaded from: /p/lib/libbtbb.so.1",
            self._firmware_line(),
            f"Firmware build: {self.compile_info}",
        ))

    @staticmethod
    def _result(command, returncode, stdout, stderr, on_output, timed_out=False):
        if on_output and stdout:
            on_output("stdout", stdout)
        if on_output and stderr:
            on_output("stderr", stderr)
        return CommandResult(command, returncode, stdout, stderr, 0.01, timed_out)

    @staticmethod
    def _crc_line(value):
        return "CRC: verify\n" if value == "1" else "CRC: DO NOT verify\n"

    def _btle(self, command, options, on_output):
        """ubertooth-btle main() at fork main 3b22c95, for the options the
        cases use: getopt with the next-word rule for -a and -k, value checks,
        the one argument rule, then the API check and the mode."""
        def fail(message, stdout=""):
            return self._result(command, 1, stdout, message + "\n" if message else "", on_output)

        def next_value(option):
            if not self.next_word:
                return None
            return {"a": not_option, "k": decimal}.get(option)

        pairs, leftover, error = getopt(options, BTLE_OPTIONS, next_value)
        if error:
            return fail(error)
        crc = None  # None: not given, "q": read, "0"/"1": set
        get_aa = set_aa = None
        with_value = set()
        for option, value in pairs:
            # opt_check_once(), before the value is looked at.
            if value is not None and self.one_value_each:
                if option in with_value:
                    return fail(f"error: option '-{option}' given more than once")
                with_value.add(option)
            if option == "k":
                if value is None:
                    crc = "q"
                elif parse_int(value, 0, 1) is None:
                    return fail(f"error: CRC verify mode must be 0-1 (got {value})")
                else:
                    crc = str(parse_int(value, 0, 1))
            elif option == "a":
                if value is None:
                    get_aa = True
                elif not re.fullmatch(r"[0-9a-fA-F]{1,8}", value):
                    return fail(f"error: access address must be 1-8 hex digits (got {value})")
                else:
                    set_aa = value.lower().rjust(8, "0")
            elif option == "A" and parse_int(value, 37, 39) is None:
                return fail(f"error: advertising channel index must be 37-39 (got {value})")
            elif option == "x" and parse_int(value, 0, 32) is None:
                return fail(f"error: access address offenses must be 0-32 (got {value})")
            elif option == "t" and value != "none" and not re.fullmatch(
                r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?:/\d+)?", value
            ):
                return fail("error: MAC address must be xx:xx:xx:xx:xx:xx"
                            f" (got {value})\nUsage:")
        if leftover and self.reject_strays:
            return fail(f"error: unexpected argument '{leftover[0]}'")
        if get_aa and set_aa:
            return fail("error: -a cannot both read and set the access address")
        if self.api < HOST_API:
            # ubertooth_check_api(): older firmware gets ubertooth_stop().
            self.stops += 1
            self._stop_mode()
            return self._result(
                command, 1, "", "Ubertooth API version found, libubertooth requires\n",
                on_output,
            )
        if sum(item in ("-f", "-n", "-p") for item in options) > 1:
            if self.reject_btle_conflicts:
                return self._result(
                    command, 1,
                    "error: choose one of -f, -n or -p, pick one pal\n", "",
                    on_output,
                )
            options = tuple(item for item in options if item != "-p")
        capture_lines = self._open_captures(options)
        if "-n" in options or "-f" in options:
            output = capture_lines
            adv = next((value for option, value in pairs if option == "A"), "37")
            self.state["channel"] = {"37": "2402", "38": "2426", "39": "2480"}[adv]
            self.mode = "ble"
            if crc not in (None, "q"):
                self.state["crc"] = "0" if self.lose_mode_crc else crc
                output = self._crc_line(crc)
            self._stop_mode()  # ubertooth_stop() on exit sends cmd_stop
            return self._result(
                command, 124, output, "HARD TIMEOUT\n", on_output, timed_out=True
            )
        output = ""
        if get_aa:
            output += f"Access address: {self.aa}\n"
        if set_aa:
            self.aa = set_aa
            output += f"access address set to: {self.aa}\n"
        if crc not in (None, "q"):
            self.state["crc"] = crc
        if crc is not None or not output:
            output += self._crc_line(self.state["crc"])
        return self._result(command, 0, output, "", on_output)

    # Headers libubertooth/libbtbb write when the capture file is created.
    PCAP_HEADER = staticmethod(lambda link: struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link))

    @staticmethod
    def _pcapng_header(link):
        # libbtbb sizes the section header block in pages (32 KiB on macOS).
        size = 0x8000
        shb = (struct.pack("<IIIHHq", 0x0A0D0D0A, size, 0x1A2B3C4D, 1, 0, -1)
               + bytes(size - 28) + struct.pack("<I", size))
        idb = struct.pack("<IIHHI", 1, 20, link, 0, 0) + struct.pack("<I", 20)
        return shb + idb

    def _open_captures(self, options):
        """ubertooth-btle getopt: -r has its own handle, -q and -c share one;
        the first option for a handle wins and extras are ignored."""
        handles = {}
        lines = ""
        for index, item in enumerate(options):
            if item not in ("-r", "-q", "-c"):
                continue
            path = options[index + 1]
            handle = "pcapng" if item == "-r" else "pcap"
            if handle in handles:
                lines += f"warning: ignoring extra capture file: {path}\n"
                continue
            handles[handle] = path
            data = (
                self._pcapng_header(256) if item == "-r"
                else self.PCAP_HEADER(256 if item == "-q" else 192)
            )
            Path(path).write_bytes(data)
        return lines

    BOOT_STATE = {
        "channel": "2441", "pa": "7", "squelch": "-120",
        "usr": "0", "rx": "0", "tx": "0", "crc": "0",
    }

    def _util(self, command, options, on_output):
        """ubertooth-util main() at fork main 3b22c95: getopt with the
        next-word rule, value checks, the one argument rule, mode checks, then
        reset, stop, configuration, reports, and the final mode."""
        def fail(message):
            return self._result(command, 1, "", message + "\n", on_output)

        def next_value(option):
            if not self.next_word:
                return None
            return signed_decimal if option == "z" else decimal

        pairs, leftover, error = getopt(options, UTIL_OPTIONS, next_value)
        if error:
            return fail(error)
        channel = pa = usr = all_leds = set_squelch = None
        get_squelch = False
        gets = set()  # values read by a separate flag (fork 5b45b3e)
        modes = []
        flags = set()
        with_value = set()
        channel_option = None
        for option, value in pairs:
            # opt_check_once() and the -c/-C conflict both run before the
            # value is looked at, so they beat a bad value and a stray word.
            if value is not None and self.one_value_each:
                if option in with_value:
                    return fail(f"error: option '-{option}' given more than once")
                with_value.add(option)
                if option in "cC":
                    if channel_option is not None:
                        return fail(f"error: -{option} cannot be combined with "
                                    f"-{channel_option}; they are the same channel "
                                    "setting written two ways")
                    channel_option = option
            if option in "lacCdqz" and value is not None:
                if option == "a":
                    if len(value) != 1 or value not in "01234567":
                        return fail(f"error: PA level must be a single digit 0-7 (got {value})")
                    parsed = int(value)
                else:
                    what, low, high, unit = UTIL_VALUES[option]
                    parsed = parse_int(value, low, high)
                    if parsed is None:
                        sep = " to " if low < 0 else "-"
                        return fail(
                            f"error: {what} must be {low}{sep}{high}{unit} (got {value})"
                        )
            read = value is None or (option == "a" and parsed == 0 and not self.pa_zero_sets)
            if read and option in "ldacC" and self.set_and_read:
                gets.add("c" if option == "C" else option)
            elif option == "l":
                usr = "q" if read else str(parsed)
            elif option == "d":
                all_leds = "q" if read else str(parsed)
            elif option == "a":
                pa = "q" if read else str(parsed)
            elif option == "c":
                channel = "q" if read else str(parsed)
            elif option == "C":
                channel = "q" if read else str(2402 + parsed)
            elif option == "z":
                if value is None:
                    get_squelch = True
                else:
                    set_squelch = str(parsed)
            elif option in "qnetxIif":
                if option not in modes:
                    modes.append(option)
            else:
                flags.add(option)
        if leftover and self.reject_strays:
            return fail(f"error: unexpected argument '{leftover[0]}'")
        # -V is the version command: since fork f898fe5 it prints the whole
        # answer and ends the command, before the mode checks and before
        # anything else on the command line runs. ``version_ends`` False
        # models the older build, where -V was one report among the others:
        # the bare build string, with no label, printed in report order.
        if "V" in flags and self.version_ends:
            return self._result(command, 0, self._version_answer(), "", on_output)
        if len(modes) > 1:
            if self.reject_conflicts:
                named = " ".join(o for o in UTIL_MODE_ORDER if o[1] in modes)
                return fail(
                    f"error: options {named} cannot be combined; give only one of "
                    + " ".join(UTIL_MODE_ORDER)
                )
            return self._result(command, 0, "", "Entering LED specan mode\n", on_output)
        if (set_squelch is not None or get_squelch) and modes:
            return fail(f"error: -z cannot be combined with -{modes[0]}")
        lines = []
        # Progress on stderr, as at fork fcadf34; stdout only what was asked.
        progress = []
        if "r" in flags:
            self.state.update(self.BOOT_STATE)
            self.mode = None
            progress.append(f"Resetting ubertooth device {self.serial}")
        if "S" in flags:
            progress.append(f"Stopping ubertooth device {self.serial}")
            self.stops += 1
            self._stop_mode()
        # Configuration in ubertooth-util's order: all LEDs, channel, USR LED, PA.
        if all_leds not in (None, "q"):
            self.state.update(usr=all_leds, rx=all_leds, tx=all_leds)
        if channel not in (None, "q"):
            self.state["channel"] = channel
        if usr not in (None, "q"):
            self.state["usr"] = usr
        if pa not in (None, "q"):
            self.state["pa"] = pa
        # Reports in ubertooth-util's order.
        if all_leds == "q" or "d" in gets:
            lines += [f"USR LED status: {self.state['usr']}",
                      f"RX LED status : {self.state['rx']}",
                      f"TX LED status : {self.state['tx']}"]
        if "b" in flags:
            lines.append("Board ID Number: 1 (Ubertooth One)")
        if channel == "q" or "c" in gets:
            mhz = self.state["channel"]
            lines.append(f"Current frequency: {mhz} MHz (Bluetooth channel {int(mhz) - 2402})")
        if "v" in flags:
            lines.append(self._firmware_line())
        if "V" in flags:
            lines.append(self.compile_info)
        if usr == "q" or "l" in gets:
            lines.append(f"USR LED status: {self.state['usr']}")
        if pa == "q" or "a" in gets:
            lines.append(f"PA Level: {self.state['pa']}")
        if "p" in flags:
            lines.append("Part ID: 1234ABCD")
        if "s" in flags:
            lines.append(f"Serial No: {self.serial}")
        if set_squelch is not None:
            # Silent since fork fcadf34.
            self.state["squelch"] = set_squelch
        if get_squelch:
            lines.append(f"Squelch set to {self.state['squelch']}")
        if "x" in modes:
            self._stop_mode()
            self.mode = "xmas"
        stdout = "".join(line + "\n" for line in lines)
        stderr = "".join(line + "\n" for line in progress)
        return self._result(command, 0, stdout, stderr, on_output)


def cases_for(test_name, coverage="smoke"):
    definition = builtin_catalog().get(test_name)
    raw = {"hosts": ["Host_Linux"], "Ubertooth": "default"}
    if "coverage" in definition.arguments:
        raw["coverage"] = coverage
    if "allow RF transmission" in definition.arguments:
        raw["allow RF transmission"] = True
    arguments = definition.normalize_arguments(raw)
    return arguments, definition.command_cases(
        arguments, {"Ubertooth": "UT_Linux0"}, IDENTITIES
    )


def run_test(test_name, executor, *, coverage="smoke", cancellation=None):
    arguments, _cases = cases_for(test_name, coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(
            f"001-{test_name}",
            test_name,
            "Host_Linux",
            arguments,
            {"Ubertooth": "UT_Linux0"},
            IDENTITIES,
        )
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        preflight = HostVersionPassthrough(executor)
        summary = NamedTestSequencer(preflight, event_sink=sink).run(
            (invocation,),
            root,
            run_id="named-run",
            manifest=manifest,
            preflight_checks=checks,
            cancellation=cancellation,
        )
        return summary, sink.events


class HostVersionPassthrough:
    """Answer the mandatory host-version preflight, forward everything else.

    The preflight asks ``ubertooth-util -V`` before any test runs, and a case
    that reads the version asks the same command later, so only the first
    call is answered here; the rest reach the device, which answers -V
    itself. One instance per run.
    """

    def __init__(self, device):
        self.device = device
        self.answered = False

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        if not self.answered and tuple(argv) == ("ubertooth-util", "-V"):
            self.answered = True
            output = "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
            return CommandResult(tuple(argv), 0, output, "", 0.01, False)
        return self.device.run(
            argv,
            timeout_s=timeout_s,
            cancellation=cancellation,
            on_output=on_output,
        )


def _cold(device):
    device.state["pa"] = "0"
    return device


def case_results(events):
    return [
        event
        for event in events
        if event.event_type == "check_finished"
        and event.details.get("phase") == "test"
    ]


def steps(events, **match):
    return [
        event
        for event in events
        if event.event_type == "step_finished"
        and all(event.details.get(key) == value for key, value in match.items())
    ]


class CoverageGenerationTests(unittest.TestCase):

    def test_compatible_pairs_are_ordered_complete_and_constrained(self):
        options = ("-a", "-b", "-c", "-d")

        pairs = compatible_pairs(options, conflicts=(("-d", "-a"),))

        expected = [
            (first, second)
            for index, first in enumerate(options)
            for second in options[index + 1:]
            if {first, second} != {"-a", "-d"}
        ]
        self.assertEqual(tuple(expected), pairs)
        self.assertEqual(pairs, compatible_pairs(options, conflicts=(("-a", "-d"),)))
        with self.assertRaisesRegex(StateCaseError, "unknown"):
            compatible_pairs(options, conflicts=(("-a", "-z"),))

    def test_case_ids_are_stable_unique_and_smoke_is_a_subset(self):
        for test_name in TRANSITION_TESTS:
            with self.subTest(test_name):
                _args, smoke = cases_for(test_name, "smoke")
                _args, comprehensive = cases_for(test_name, "comprehensive")
                _args, again = cases_for(test_name, "comprehensive")
                smoke_ids = [case.case_id for case in smoke]
                full_ids = [case.case_id for case in comprehensive]
                self.assertEqual(full_ids, [case.case_id for case in again])
                self.assertEqual(len(full_ids), len(set(full_ids)))
                self.assertLess(len(smoke_ids), len(full_ids))
                self.assertTrue(set(smoke_ids) <= set(full_ids))

    def test_comprehensive_compositions_cover_every_compatible_pair(self):
        options = tuple(option for option, _label, _pattern in transitions._QUERY_OPTIONS)
        _args, cases = cases_for("Ubertooth util compositions", "comprehensive")

        # -V does not compose: its case is a pair of options but not a
        # composition, so it is left out of the pair set.
        composed = {frozenset(case.argv[3:]) for case in cases
                    if len(case.argv) == 5 and case.case_id != transitions.VERSION_CASE}

        self.assertEqual(
            {frozenset(pair) for pair in compatible_pairs(options)}, composed
        )
        self.assertNotIn("-V", options)
        full = next(case for case in cases if case.case_id == "full-readback-composition")
        self.assertEqual(options, full.argv[3:])
        version = next(case for case in cases
                       if case.case_id == transitions.VERSION_CASE)
        self.assertEqual(("-V", "-s"), version.argv[3:])

    def test_plans_select_valid_profiles_for_every_transition_test(self):
        expected = {"fast.yaml": ("fast", "smoke"), "release.yaml": ("release", "comprehensive")}
        for filename, (suite, profile) in expected.items():
            plan = load_plan(PROJECT / "testplans" / filename)
            selected = {
                step.test_name: step.arguments["coverage"]
                for step in plan.steps(suite)
                if step.test_name in TRANSITION_TESTS
            }
            with self.subTest(filename):
                self.assertEqual(set(TRANSITION_TESTS), set(selected))
                self.assertEqual({profile}, set(selected.values()))

    def test_coverage_profile_is_required(self):
        definition = builtin_catalog().get("Radio setting transitions")

        with self.assertRaisesRegex(CatalogError, "missing required argument 'coverage'"):
            definition.normalize_arguments({"hosts": "each"})

    def test_repeated_channel_index_is_refused_and_changes_nothing(self):
        _args, cases = cases_for("Radio setting transitions", "comprehensive")
        case = next(c for c in cases if c.case_id == "channel-index-repeated-set-refused")
        context = {"original channel-index": "2441"}
        step = next(s for s in case.steps if s.step_id == "set-twice")

        self.assertEqual(("-C48", "-C24"), step.resolve_argv(context)[3:])
        self.assertEqual((1,), step.expected_returncodes)
        self.assertEqual((r"(?m)^error: option '\-C' given more than once$",),
                         step.output_patterns)
        unchanged = next(s for s in case.steps if s.step_id == "verify-unchanged")
        self.assertEqual("2441", unchanged.observe.expected_value(context))

    def test_internal_error_still_runs_cleanup_then_propagates(self):
        from unittest import mock
        from ubertooth_harness.events import JsonlEventSink

        device = FakeUbertooth()
        original_emit = JsonlEventSink.emit

        def failing_emit(sink, event):
            if (
                event.event_type == "step_finished"
                and event.details.get("step_id") == "set"
            ):
                raise OSError("disk full")
            original_emit(sink, event)

        arguments, _cases = cases_for("Radio setting transitions")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Radio setting transitions", "Radio setting transitions",
                "Host_Linux", arguments, {"Ubertooth": "UT_Linux0"}, IDENTITIES,
            )
            manifest, checks = run_contract(root, invocation)
            original_channel = device.state["channel"]
            with mock.patch.object(JsonlEventSink, "emit", failing_emit):
                with self.assertRaisesRegex(OSError, "disk full"):
                    NamedTestSequencer(HostVersionPassthrough(device)).run(
                        (invocation,), root, run_id="named-run",
                        manifest=manifest, preflight_checks=checks,
                    )

        self.assertEqual(original_channel, device.state["channel"])
        self.assertIn(
            ("ubertooth-util", "-U", SERIAL, f"-c{original_channel}"), device.commands
        )

    def test_unknown_coverage_profile_is_rejected(self):
        definition = builtin_catalog().get("BLE CRC transitions")

        with self.assertRaisesRegex(CatalogError, "coverage"):
            definition.normalize_arguments({"coverage": "everything"})

    def test_state_case_requires_cleanup_and_unique_steps(self):
        step = Step("read", "Read", ("tool",))
        with self.assertRaisesRegex(StateCaseError, "cleanup"):
            StateCase("case", "Case", (step,), ())
        with self.assertRaisesRegex(StateCaseError, "duplicate step"):
            StateCase("case", "Case", (step,), (step,))
        with self.assertRaisesRegex(StateCaseError, "invalid case ID"):
            StateCase("Bad Case", "Case", (step,), (Step("c", "C", ("tool",)),))
        with self.assertRaisesRegex(StateCaseError, "value"):
            Observation("crc", r"CRC: (verify)")

    def test_optional_arguments_are_attached_and_device_selected_by_serial(self):
        for test_name in TRANSITION_TESTS + ("Ubertooth util conflicts", "BLE mode conflicts"):
            _args, cases = cases_for(test_name, "comprehensive")
            context = {
                "original crc": "0",
                "original channel": "2441",
                "original pa-level": "7",
                "original squelch": "-90",
                "original usr-led": "1",
                "original leds": "1/0/0",
                "original channel-index": "2441",
                "capture dir": "/tmp/captures",
            }
            for case in cases:
                if isinstance(case, StateCase):
                    commands = [
                        step.resolve_argv(context)
                        for step in case.steps + case.cleanup
                    ]
                else:
                    commands = [case.argv]
                # Host-side capture header checks select no device.
                commands = [c for c in commands if c[0] != sys.executable]
                for command in commands:
                    with self.subTest(test_name, case=case.case_id, argv=command):
                        selections = [
                            command[index + 1]
                            for index, item in enumerate(command)
                            if item == "-U"
                        ]
                        self.assertEqual(SERIAL, selections[-1])
                        # The reads are written out, never bundled.
                        self.assertNotIn("-vspb", command)


class TransitionExecutionTests(unittest.TestCase):

    def test_capture_cases_cover_each_format_mode_and_crc(self):
        _args, smoke = cases_for("BLE CRC transitions", "smoke")
        _args, full = cases_for("BLE CRC transitions", "comprehensive")
        smoke_ids = {case.case_id for case in smoke}
        full_ids = {case.case_id for case in full}
        for label in ("pcapng", "pcap-le-phdr", "pcap-ppi"):
            self.assertIn(f"crc-enabled-during-no-follow-capture-{label}", smoke_ids)
            for mode in ("no-follow", "follow"):
                for state in ("enabled", "disabled"):
                    self.assertIn(f"crc-{state}-during-{mode}-capture-{label}", full_ids)
        # -q and -c share one handle; the same option twice is refused, so
        # only the two mixed pairs are first-option-wins.
        for pair in ("pcap-le-phdr-then-pcap-ppi", "pcap-ppi-then-pcap-le-phdr"):
            self.assertIn(f"capture-repeated-{pair}-first-wins", full_ids)
        self.assertNotIn("capture-repeated-pcapng-then-pcapng-first-wins", full_ids)
        self.assertIn("capture-pcapng-and-pcap-both-written", full_ids)
        self.assertTrue(all(c.capture_dir for c in full if "capture" in c.case_id))

    def test_capture_cases_verify_link_types_and_first_option_wins(self):
        device = FakeUbertooth(crc="1")

        summary, events = run_test("BLE CRC transitions", device, coverage="comprehensive")

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(12, len(steps(events, step_id="verify-capture")))
        observed = {
            e.details["case_id"]: e.details["observed"]
            for e in steps(events, step_id="verify-capture")
        }
        for case_id, value in observed.items():
            expected = "pcap, link type: 192" if case_id.endswith("pcap-ppi") else (
                "pcapng, link type: 256" if case_id.endswith("pcapng") else "pcap, link type: 256"
            )
            self.assertEqual(expected, value, case_id)
        self.assertEqual(2, len([e for e in steps(events, step_id="verify-second-absent")
                                 if e.status == "passed"]))
        first = {
            e.details["case_id"]: e.details["observed"]
            for e in steps(events, step_id="verify-first")
        }
        self.assertEqual(
            ["pcap, link type: 192", "pcap, link type: 256"],
            sorted(first.values()),
        )
        mode_argv = [
            e.details["argv"] for e in steps(events, step_id="mode")
            if "-q" in e.details["argv"] or "-r" in e.details["argv"] or "-c" in e.details["argv"]
        ]
        for argv in mode_argv:
            for path in (argv[i + 1] for i, a in enumerate(argv) if a in ("-r", "-q", "-c")):
                self.assertIn("/captures/001-BLE-CRC-transitions/", path)

    def test_a_wrong_capture_link_type_fails_the_case(self):
        device = FakeUbertooth(crc="1")
        device.PCAP_HEADER = staticmethod(
            lambda link: struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        )

        summary, events = run_test("BLE CRC transitions", device)

        self.assertFalse(summary.passed)
        failed = [e for e in steps(events, step_id="verify-capture") if e.status == "failed"]
        self.assertEqual(2, len(failed))  # -q and -c in smoke; -r is pcapng
        self.assertIn("link type: 1", failed[0].details["observed"])

    def test_crc_transitions_pass_and_restore_original(self):
        device = FakeUbertooth(crc="1")

        summary, events = run_test("BLE CRC transitions", device)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual("1", device.state["crc"])
        effective = steps(events, step_id="verify-effective")
        self.assertTrue(effective)
        for event in effective:
            self.assertEqual("1", event.details["expected"])
            self.assertEqual("1", event.details["observed"])
            self.assertEqual(["ubertooth-btle", "-U", SERIAL, "-k"], event.details["argv"])

    def test_continuous_mode_deadline_reports_stopped_not_failed(self):
        _summary, events = run_test("BLE CRC transitions", FakeUbertooth(crc="1"))

        mode = [
            e for e in events
            if e.event_type == "command_finished" and e.details.get("step_id") == "mode"
        ]
        self.assertTrue(mode)
        self.assertTrue(all(e.status == "stopped" for e in mode))

    def test_crc_lost_after_mode_start_cannot_falsely_pass(self):
        device = FakeUbertooth(crc="0", lose_mode_crc=True)

        summary, events = run_test("BLE CRC transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("expected crc after mode=1, observed 0", summary.failure_reason)
        finished = case_results(events)
        _args, cases = cases_for("BLE CRC transitions")
        self.assertEqual(len(cases), len(finished), "restored failures continue")
        for event in finished:
            self.assertEqual("failed", event.status)
            self.assertEqual("verify-effective", event.details["failed_step"])
            self.assertEqual("passed", event.details["cleanup_status"])
        self.assertEqual("0", device.state["crc"])

    def test_radio_settings_restore_after_successful_transitions(self):
        device = FakeUbertooth()
        original = dict(device.state)

        summary, _events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(original, device.state)
        self.assertIn(("ubertooth-util", "-U", SERIAL, "-c2426"), device.commands)

    def test_failed_mutation_is_not_masked_by_later_success(self):
        device = FakeUbertooth(fail=lambda argv: argv[-1] == "-c2426")

        summary, events = run_test("Radio setting transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("Set different channel: command exited 1", summary.failure_reason)
        self.assertNotIn("cleanup failed", summary.failure_reason)
        channel_case = "001-Radio setting transitions-channel-mode-stop-restore"
        transition_ids = [
            e.details["step_id"]
            for e in steps(events, stage="transition", case_id=channel_case)
        ]
        self.assertEqual(
            ["establish-idle", "read-original-channel", "read-original-squelch",
             "read-original-leds", "set"],
            transition_ids,
        )
        cleanup = steps(events, stage="cleanup", case_id=channel_case)
        self.assertEqual(
            [
                "cleanup-stop",
                "restore-channel",
                "verify-restored-channel",
                "restore-squelch",
                "verify-restored-squelch",
                "restore-leds",
                "verify-restored-leds",
                "restored-device-identity",
            ],
            [event.details["step_id"] for event in cleanup],
        )
        self.assertTrue(all(event.status == "passed" for event in cleanup))
        self.assertEqual("failed", summary.status)

    def test_cleanup_failure_fails_an_otherwise_passing_case(self):
        device = FakeUbertooth(fail=lambda argv: argv[-1] == "-c2441")

        summary, _events = run_test("Radio setting transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("cleanup failed: Restore original channel", summary.failure_reason)

    def test_unknown_original_skips_restore_without_hiding_read_failure(self):
        device = FakeUbertooth(fail=lambda argv: argv == ("ubertooth-btle", "-U", SERIAL, "-k"))

        summary, events = run_test("BLE CRC transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("Read original CRC: command exited 1", summary.failure_reason)
        self.assertNotIn("cleanup failed", summary.failure_reason)
        restore = steps(events, step_id="restore-crc")
        self.assertEqual("skipped", restore[0].status)
        self.assertFalse(any(command[-1] in ("-k0", "-k1") for command in device.commands))

    def test_cancellation_during_mode_still_runs_cleanup(self):
        token = CancellationToken()
        device = FakeUbertooth(
            crc="1", cancel_on=lambda argv: "-n" in argv
        )

        summary, events = run_test(
            "BLE CRC transitions", device, cancellation=token
        )

        self.assertEqual("canceled", summary.status)
        cleanup = steps(events, stage="cleanup")
        self.assertEqual(
            [
                "cleanup-stop",
                "restore-crc",
                "verify-restored-crc",
                "restore-channel",
                "verify-restored-channel",
                "restore-squelch",
                "verify-restored-squelch",
                "restore-leds",
                "verify-restored-leds",
                "restored-device-identity",
            ],
            [event.details["step_id"] for event in cleanup],
        )
        self.assertTrue(all(event.status == "passed" for event in cleanup))
        self.assertEqual("1", device.state["crc"])
        cleanup_tokens = device.tokens[-len(cleanup):]
        self.assertTrue(all(not item.is_cancelled() for item in cleanup_tokens))
        self.assertFalse(
            any(e.event_type == "test_finished" and e.status == "passed" for e in events)
        )

    def test_events_identify_case_step_resource_and_cleanup(self):
        device = FakeUbertooth()

        _summary, events = run_test("Radio setting transitions", device)

        started = next(
            e for e in events
            if e.event_type == "check_started" and e.details.get("state_changing")
        )
        self.assertEqual(IDENTITIES, started.details["resource_identities"])
        self.assertTrue(started.details["expected_state"])
        command = next(
            e for e in events
            if e.event_type == "command_started" and e.details.get("step_id") == "set"
        )
        self.assertEqual("transition", command.details["stage"])
        self.assertEqual("001-Radio setting transitions-channel-mode-stop-restore", command.details["case_id"])

    def test_conflicting_modes_must_be_rejected_before_starting(self):
        accepted, _events = run_test(
            "Ubertooth util conflicts", FakeUbertooth(reject_conflicts=False)
        )
        rejected, _events = run_test(
            "Ubertooth util conflicts", FakeUbertooth(reject_conflicts=True)
        )

        self.assertFalse(accepted.passed)
        self.assertIn("command exited 0", accepted.failure_reason)
        self.assertTrue(rejected.passed, rejected.failure_reason)

    def test_compositions_assert_every_report(self):
        summary, _events = run_test(
            "Ubertooth util compositions", FakeUbertooth(), coverage="comprehensive"
        )

        self.assertTrue(summary.passed, summary.failure_reason)

    def test_a_v_that_does_not_end_the_command_fails_the_version_case(self):
        # Before fork f898fe5 -V was one report among the others, so -V -s
        # printed the serial too and no version answer. Harness #162.
        summary, events = run_test(
            "Ubertooth util compositions", FakeUbertooth(version_ends=False),
            coverage="comprehensive",
        )

        self.assertFalse(summary.passed)
        failed = sorted(
            event.details["case_id"].rsplit("compositions-", 1)[1]
            for event in case_results(events) if event.status == "failed"
        )
        self.assertEqual([transitions.VERSION_CASE], failed)
        self.assertIn("-V beside a query", summary.failure_reason)

    def test_compositions_do_not_repeat_identical_commands(self):
        _args, cases = cases_for("Ubertooth util compositions", "comprehensive")

        commands = [case.argv for case in cases]

        self.assertEqual(len(commands), len({frozenset(c) for c in commands}))

    def test_a_board_at_pa_zero_runs_every_case_and_gets_pa_zero_back(self):
        # Harness #5: -a0 sets PA 0 (fork 4ed4a1d), so nothing skips and the
        # reset case covers a board at PA 0.
        device = FakeUbertooth()
        device.state["pa"] = "0"

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(0, summary.skipped_cases)
        self.assertEqual("0", device.state["pa"])
        reset = "001-Radio setting transitions-full-reset-restores-boot-defaults"
        after = steps(events, step_id="verify-pa-level-after-reset", case_id=reset)[0]
        self.assertEqual("7", after.details["observed"])
        restore = steps(events, step_id="restore-pa-level", case_id=reset)[0]
        self.assertEqual(["ubertooth-util", "-U", SERIAL, "-a0"], restore.details["argv"])
        self.assertEqual("passed", restore.status)

    def test_every_led_is_restored_after_repeated_all_led_set(self):
        device = FakeUbertooth()
        device.state.update(usr="1", rx="1", tx="1")

        summary, events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(("1", "1", "1"), (device.state["usr"], device.state["rx"], device.state["tx"]))
        case = "001-Radio setting transitions-all-leds-repeated-set-refused"
        unchanged = steps(events, step_id="verify-unchanged", case_id=case)[0]
        self.assertEqual("1/1/1", unchanged.details["observed"])

    def test_a_repeated_set_that_is_taken_fails_its_case(self):
        """Before opt_check_once() the last value won. Every repeated-set case
        has to notice, whatever the board's original value is."""
        for original in ("0", "1"):
            device = FakeUbertooth(one_value_each=False)
            device.state["usr"] = original

            summary, events = run_test(
                "Radio setting transitions", device, coverage="comprehensive"
            )

            with self.subTest(original=original):
                self.assertFalse(summary.passed)
                self.assertEqual(
                    sorted(["all-leds-repeated-set-refused",
                            "channel-index-repeated-set-refused",
                            "channel-index-then-mhz", "channel-mhz-then-index",
                            "channel-repeated-set-refused",
                            "pa-level-repeated-set-refused",
                            "squelch-repeated-set-refused",
                            "usr-led-repeated-set-refused"]),
                    failed_ids(events, "Radio setting transitions"))

    def test_unrestorable_original_skips_cases_before_any_change(self):
        for leds in (("1", "0"), ("0", "1")):
            device = FakeUbertooth()
            device.state.update(rx=leds[0], tx=leds[1])
            before = dict(device.state)

            summary, events = run_test(
                "Radio setting transitions", device, coverage="comprehensive"
            )

            touching = [
                e for e in case_results(events)
                if any(
                    s.details["step_id"] == "read-original-leds"
                    for s in steps(events, case_id=e.details["case_id"])
                )
            ]
            with self.subTest(leds=leds):
                self.assertTrue(touching)
                self.assertTrue(all(e.status == "skipped" for e in touching))
                self.assertIn("cannot be restored", touching[0].details["reason"])
                self.assertEqual(before["rx"], device.state["rx"])
                self.assertEqual(before["tx"], device.state["tx"])
                self.assertGreaterEqual(summary.skipped_cases, len(touching))
                self.assertTrue(summary.passed, summary.failure_reason)

    def test_usr_led_mode_stop_check_is_real_when_original_is_on(self):
        device = FakeUbertooth()
        device.state["usr"] = "1"
        device.IDLE_RESET = {"channel": "2441", "squelch": "-120", "rx": "0", "tx": "0"}

        summary, _events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertFalse(summary.passed)
        self.assertIn("usr-led after mode stop=0, observed 1", summary.failure_reason)

    def test_unrestorable_value_after_a_change_fails_cleanup(self):
        from types import MappingProxyType
        from ubertooth_harness.catalog import TestCatalog, TestDefinition
        from ubertooth_harness.stateful import Unrestorable

        def unrestorable(_context):
            raise Unrestorable("leds 1/0/1 cannot be set by ubertooth-util")

        case = StateCase(
            "changed-then-unrestorable",
            "Changed then unrestorable",
            (Step("change", "Change LEDs", ("ubertooth-util", "-U", SERIAL, "-d1"), mutates=True),),
            (Step("restore", "Restore LEDs", unrestorable),),
        )
        catalog = TestCatalog([
            TestDefinition(
                "Custom",
                "custom",
                MappingProxyType({}),
                MappingProxyType({"Ubertooth": "Ubertooth"}),
                command_builder=lambda *_args: (case,),
            )
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Custom", "Custom", "Host_Linux", {},
                {"Ubertooth": "UT_Linux0"}, IDENTITIES,
            )
            manifest, checks = run_contract(root, invocation)
            sink = CollectSink()
            summary = NamedTestSequencer(
                HostVersionPassthrough(FakeUbertooth()), catalog=catalog, event_sink=sink
            ).run(
                (invocation,), root, run_id="named-run",
                manifest=manifest, preflight_checks=checks,
            )

        self.assertFalse(summary.passed)
        self.assertIn("cleanup failed: Restore LEDs: skipped, leds 1/0/1", summary.failure_reason)
        self.assertEqual("failed", case_results(sink.events)[0].details["cleanup_status"])

    def test_missing_original_after_a_change_is_a_failed_step(self):
        case = StateCase(
            "changed-without-original",
            "Changed without a recorded original",
            (Step("change", "Change channel", ("ubertooth-util", "-U", SERIAL, "-c2426"), mutates=True),),
            (transitions.CHANNEL.restore(SERIAL)[0],),
        )
        from types import MappingProxyType
        from ubertooth_harness.catalog import TestCatalog, TestDefinition

        catalog = TestCatalog([
            TestDefinition(
                "Custom", "custom", MappingProxyType({}),
                MappingProxyType({"Ubertooth": "Ubertooth"}),
                command_builder=lambda *_args: (case,),
            )
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Custom", "Custom", "Host_Linux", {},
                {"Ubertooth": "UT_Linux0"}, IDENTITIES,
            )
            manifest, checks = run_contract(root, invocation)
            sink = CollectSink()
            summary = NamedTestSequencer(
                HostVersionPassthrough(FakeUbertooth()), catalog=catalog, event_sink=sink
            ).run(
                (invocation,), root, run_id="named-run",
                manifest=manifest, preflight_checks=checks,
            )

        self.assertFalse(summary.passed)
        restore = steps(sink.events, step_id="restore-channel")[0]
        self.assertEqual("failed", restore.status)
        self.assertIn("missing recorded original channel", restore.details["reason"])

    def test_full_reset_moves_every_checked_value_off_its_default_first(self):
        device = FakeUbertooth()
        device.state.update(channel="2441", squelch="-120", usr="0", rx="0", tx="0")
        device.BOOT_STATE = dict(device.BOOT_STATE, squelch="-60")

        summary, _events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertFalse(summary.passed)
        self.assertIn("squelch after reset=-120, observed -60", summary.failure_reason)

    def test_every_led_is_checked_off_after_a_mode_stop(self):
        device = FakeUbertooth()
        device.IDLE_RESET = {"channel": "2441", "squelch": "-120", "usr": "0", "rx": "0"}

        summary, _events = run_test("Radio setting transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("leds after mode stop=0/0/0, observed 0/0/1", summary.failure_reason)

    def test_util_conflicts_run_every_pair_on_a_board_at_pa_zero(self):
        summary, events = run_test(
            "Ubertooth util conflicts", _cold(FakeUbertooth(reject_conflicts=True))
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(15, sum(e.status == "passed" for e in case_results(events)))

    def test_reset_case_sets_pa_zero_before_the_reset(self):
        for original in ("5", "0", "7"):
            device = FakeUbertooth()
            device.state["pa"] = original

            summary, events = run_test(
                "Radio setting transitions", device, coverage="comprehensive"
            )

            with self.subTest(original=original):
                self.assertTrue(summary.passed, summary.failure_reason)
                before = steps(events, step_id="verify-pa-level-before-reset")[0]
                self.assertEqual("0", before.details["observed"])
                self.assertEqual(original, device.state["pa"])

    def test_pa_zero_cases_fail_where_a0_is_a_query(self):
        # Fork builds before 4ed4a1d read PA for -a0 and -a 0.
        device = FakeUbertooth(pa_zero_sets=False)

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        failed = sorted(
            e.details["case_id"].rsplit("transitions-", 1)[1]
            for e in case_results(events) if e.status == "failed"
        )
        self.assertEqual(
            ["full-reset-restores-boot-defaults", "pa-level-zero-attached",
             "pa-level-zero-next-word"],
            failed,
        )

    def test_full_reset_must_return_pa_to_its_boot_default(self):
        kept = FakeUbertooth()
        changed = FakeUbertooth()
        changed.BOOT_STATE = dict(changed.BOOT_STATE, pa="3")

        kept_summary, _events = run_test(
            "Radio setting transitions", kept, coverage="comprehensive"
        )
        changed_summary, _events = run_test(
            "Radio setting transitions", changed, coverage="comprehensive"
        )

        self.assertTrue(kept_summary.passed, kept_summary.failure_reason)
        self.assertFalse(changed_summary.passed)
        self.assertIn("pa-level after reset=7, observed 3", changed_summary.failure_reason)

    def test_util_conflicts_require_explicit_rf_opt_in(self):
        definition = builtin_catalog().get("Ubertooth util conflicts")

        with self.assertRaisesRegex(CatalogError, "allow RF transmission"):
            definition.normalize_arguments({"hosts": "each"})
        with self.assertRaisesRegex(CatalogError, "must be true"):
            definition.normalize_arguments({"allow RF transmission": False})

    def test_case_build_errors_fail_before_any_hardware_access(self):
        device = FakeUbertooth()
        arguments, _cases = cases_for("Radio setting transitions")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Radio setting transitions", "Radio setting transitions",
                "Host_Linux", arguments, {"Ubertooth": "UT_Linux0"}, {},
            )
            manifest, checks = run_contract(root, invocation)
            with self.assertRaisesRegex(ValueError, "cannot build test cases"):
                NamedTestSequencer(HostVersionPassthrough(device)).run(
                    (invocation,), root, run_id="named-run",
                    manifest=manifest, preflight_checks=checks,
                )
            self.assertFalse((root / "run.jsonl").exists())
        self.assertEqual([], device.commands)

    def test_event_failure_during_cleanup_still_finishes_every_restore(self):
        from unittest import mock
        from ubertooth_harness.events import JsonlEventSink

        device = FakeUbertooth()
        original_state = dict(device.state)
        original_emit = JsonlEventSink.emit

        def failing_emit(sink, event):
            if (
                event.event_type == "step_finished"
                and event.details.get("step_id") == "restore-channel"
            ):
                raise OSError("disk full")
            original_emit(sink, event)

        arguments, _cases = cases_for("Radio setting transitions")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invocation = TestInvocation(
                "001-Radio setting transitions", "Radio setting transitions",
                "Host_Linux", arguments, {"Ubertooth": "UT_Linux0"}, IDENTITIES,
            )
            manifest, checks = run_contract(root, invocation)
            with mock.patch.object(JsonlEventSink, "emit", failing_emit):
                with self.assertRaisesRegex(OSError, "disk full"):
                    NamedTestSequencer(HostVersionPassthrough(device)).run(
                        (invocation,), root, run_id="named-run",
                        manifest=manifest, preflight_checks=checks,
                    )

        self.assertEqual(original_state, device.state)

    def test_btle_mode_conflicts_pass_only_when_rejected(self):
        rejected, events = run_test("BLE mode conflicts", FakeUbertooth())
        accepted, _events = run_test(
            "BLE mode conflicts", FakeUbertooth(reject_btle_conflicts=False)
        )

        self.assertTrue(rejected.passed, rejected.failure_reason)
        self.assertEqual(4, len(case_results(events)))
        self.assertFalse(accepted.passed)

    def test_pattern_failures_name_the_pattern(self):
        device = FakeUbertooth(reject_btle_conflicts=True)
        original = device._btle

        def wrong_message(command, options, on_output):
            result = original(command, options, on_output)
            if result.returncode == 1:
                return CommandResult(result.argv, 1, "nope\n", "", 0.01, False)
            return result

        device._btle = wrong_message

        summary, _events = run_test("BLE mode conflicts", device)

        self.assertIn("'error: choose one of -f, -n or -p'", summary.failure_reason)

    def test_settings_reset_when_a_mode_stops_but_not_from_idle(self):
        device = FakeUbertooth()

        summary, events = run_test("Radio setting transitions", device)

        self.assertTrue(summary.passed, summary.failure_reason)
        channel_case = "001-Radio setting transitions-channel-mode-stop-restore"
        idle = steps(events, step_id="verify-after-idle-stop", case_id=channel_case)[0]
        mode = steps(events, step_id="verify-after-mode-stop", case_id=channel_case)[0]
        self.assertEqual("2426", idle.details["observed"])
        self.assertEqual("2441", mode.details["observed"])
        pa_case = "001-Radio setting transitions-pa-level-mode-stop-restore"
        pa = steps(events, step_id="verify-after-mode-stop", case_id=pa_case)[0]
        self.assertEqual("5", pa.details["observed"])

    def test_fake_without_idle_reset_cannot_pass_mode_stop_checks(self):
        device = FakeUbertooth()
        device.IDLE_RESET = {}

        summary, _events = run_test("Radio setting transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("after mode stop", summary.failure_reason)

    def test_full_reset_returns_boot_defaults_then_restores_everything(self):
        device = FakeUbertooth(crc="1")
        original = dict(device.state)

        summary, events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(original, device.state)
        after_reset = steps(events, step_id="verify-channel-after-reset")
        self.assertEqual("2441", after_reset[0].details["observed"])
        self.assertIn(("ubertooth-util", "-U", SERIAL, "-r"), device.commands)

    def test_failed_set_with_successful_report_must_exit_nonzero(self):
        masked = FakeUbertooth()
        masked_run = masked._util

        def mask_status(command, options, on_output):
            result = masked_run(command, options, on_output)
            return CommandResult(
                result.argv, 0, result.stdout, result.stderr, 0.01, False
            )

        masked._util = mask_status

        summary, _events = run_test(
            "Radio setting transitions", masked, coverage="comprehensive"
        )

        self.assertFalse(summary.passed)
        self.assertIn("Invalid -a9 followed by -v: command exited 0", summary.failure_reason)

    def test_every_conflict_pair_runs_after_a_restored_failure(self):
        device = FakeUbertooth(reject_conflicts=False)

        summary, events = run_test("Ubertooth util conflicts", device)

        _args, cases = cases_for("Ubertooth util conflicts")
        self.assertEqual(15, len(cases))
        self.assertEqual(len(cases), len(case_results(events)))
        self.assertFalse(summary.passed)
        options = {c[3] for c in device.commands if len(c) == 5}
        self.assertTrue({"-q", "-n", "-e", "-t", "-x", "-I"} <= options | {c[4] for c in device.commands if len(c) == 5})
        self.assertFalse(any({"-i", "-f"} & set(c) for c in device.commands))
        self.assertIn(("ubertooth-util", "-U", SERIAL, "-d0", "-l1"), device.commands)

    def test_unrestored_failure_stops_the_group(self):
        device = FakeUbertooth(
            reject_conflicts=False,
            fail=lambda argv: argv[-2:] == ("-d0", "-l1"),
        )

        _summary, events = run_test("Ubertooth util conflicts", device)

        self.assertEqual(1, len(case_results(events)))
        self.assertEqual("failed", case_results(events)[0].details["cleanup_status"])

    def test_cancellation_does_not_hide_a_cleanup_failure(self):
        token = CancellationToken()
        device = FakeUbertooth(
            crc="1",
            cancel_on=lambda argv: "-n" in argv,
            fail=lambda argv: argv == ("ubertooth-btle", "-U", SERIAL, "-k1"),
        )

        summary, events = run_test("BLE CRC transitions", device, cancellation=token)

        self.assertEqual("failed", summary.status)
        self.assertEqual("failed", case_results(events)[0].status)
        self.assertIn("cleanup failed: Restore original CRC", summary.failure_reason)

    def test_cancellation_before_a_step_is_recorded_for_that_step(self):
        token = CancellationToken()
        device = FakeUbertooth(
            crc="1",
            cancel_on=lambda argv: argv[-1] == "-k0",
        )

        summary, events = run_test("BLE CRC transitions", device, cancellation=token)

        self.assertEqual("canceled", summary.status)
        failed_step = case_results(events)[0].details["failed_step"]
        recorded = steps(events, step_id=failed_step)
        self.assertEqual(1, len(recorded))
        self.assertEqual("canceled", recorded[0].status)


NEXT_WORD_CASES = (
    "channel-next-word-value", "channel-index-next-word-value",
    "pa-level-next-word-value", "squelch-next-word-value",
    "usr-led-next-word-value", "leds-next-word-value",
    "pa-level-zero-next-word", "next-word-values-in-one-command",
)


def failed_ids(events, test_name):
    return sorted(
        e.details["case_id"].rsplit(f"{test_name.replace(' ', '-')}-", 1)[-1]
        .rsplit(f"{test_name}-", 1)[-1]
        for e in case_results(events) if e.status == "failed"
    )


class OptionRuleTests(unittest.TestCase):
    """Next-word values and PA 0 against a fake of fork main 3b22c95."""

    def run_util(self, device, *options):
        return device.run(("ubertooth-util", "-U", SERIAL) + options, timeout_s=1)

    def test_fake_follows_the_fork_argument_rule(self):
        device = FakeUbertooth()
        self.assertEqual(0, self.run_util(device, "-c", "2426").returncode)
        self.assertEqual("2426", device.state["channel"])
        both = self.run_util(device, "-c", "-b")  # an option is never a value
        self.assertIn("Current frequency: 2426 MHz", both.stdout)
        self.assertIn("Board ID Number", both.stdout)
        self.assertEqual(0, self.run_util(device, "-z", "-70").returncode)
        self.assertEqual("-70", device.state["squelch"])
        self.assertEqual(0, self.run_util(device, "-a", "0").returncode)
        self.assertEqual("0", device.state["pa"])
        for options, word in ((("-a", "7x"), "7x"), (("-z", "-"), "-"),
                              (("-v", "--", "-c2402"), "-c2402"), (("-c2470", "stray"), "stray")):
            result = self.run_util(device, *options)
            with self.subTest(options=options):
                self.assertEqual(1, result.returncode)
                self.assertEqual(f"error: unexpected argument '{word}'\n", result.stderr)
        self.assertEqual("2426", device.state["channel"])
        # SqueakyFloors/ubertooth#228 (fixed in 5b45b3e): the set runs, then the read.
        self.assertIn("2441 MHz", self.run_util(device, "-c2441", "-c").stdout)
        self.assertEqual("2441", device.state["channel"])
        old = FakeUbertooth(set_and_read=False)
        self.assertIn("2441 MHz", self.run_util(old, "-c2426", "-c").stdout)
        self.assertEqual("2441", old.state["channel"])

    def test_set_and_read_cases_need_the_228_fix(self):
        set_and_read = {"channel-set-and-read": ("-c2426", "-c"),
                        "channel-read-and-set": ("-c", "-c2426"),
                        "pa-level-set-and-read": ("-a5", "-a"),
                        "usr-led-set-and-read": ("-l0", "-l"),
                        "all-leds-set-and-read": ("-d1", "-d")}
        device = FakeUbertooth()
        original = dict(device.state)
        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(original, device.state)
        sets = {
            e.details["case_id"].rsplit("transitions-", 1)[1]: tuple(e.details["argv"][3:])
            for e in steps(events, step_id="set-and-read")
        }
        self.assertEqual(set_and_read, sets)
        old = FakeUbertooth(set_and_read=False)
        summary, events = run_test("Radio setting transitions", old, coverage="comprehensive")
        self.assertFalse(summary.passed)
        failed = sorted(e.details["case_id"].rsplit("transitions-", 1)[1] for e in events
                        if e.event_type == "check_finished" and e.status == "failed"
                        and e.details.get("phase") == "test")
        self.assertEqual(sorted(set_and_read), failed)
        _summary, events = run_test("Radio setting transitions", FakeUbertooth())
        self.assertEqual([], [e for e in steps(events, step_id="set-and-read")])

    def test_util_progress_on_stdout_before_fcadf34_fails(self):
        # Fork #326: -S, -r and the setters print nothing on stdout.
        class Before(FakeUbertooth):
            @staticmethod
            def _result(command, returncode, stdout, stderr, on_output, timed_out=False):
                if command[0] == "ubertooth-util":
                    stdout, stderr = stdout + stderr, ""
                return FakeUbertooth._result(command, returncode, stdout, stderr,
                                             on_output, timed_out)

        summary, _events = run_test("Radio setting transitions", Before())
        self.assertFalse(summary.passed)
        self.assertIn("'Stopping ubertooth device", summary.failure_reason)
        self.assertIn("progress goes to stderr", summary.failure_reason)

    def test_next_word_cases_pass_and_restore_on_fork_main(self):
        device = FakeUbertooth()
        original = dict(device.state)

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(original, device.state)
        sets = {
            e.details["case_id"].rsplit("transitions-", 1)[1]: tuple(e.details["argv"][3:])
            for e in steps(events, step_id="set")
        }
        self.assertEqual(("-c", "2426"), sets["channel-next-word-value"])
        self.assertEqual(("-C", "24"), sets["channel-index-next-word-value"])
        self.assertEqual(("-a", "5"), sets["pa-level-next-word-value"])
        self.assertEqual(("-z", "-60"), sets["squelch-next-word-value"])
        self.assertEqual(("-l", "1"), sets["usr-led-next-word-value"])
        self.assertEqual(("-d", "1"), sets["leds-next-word-value"])
        self.assertEqual(("-a", "0"), sets["pa-level-zero-next-word"])
        self.assertEqual(("-a0",), sets["pa-level-zero-attached"])
        self.assertEqual(("-c", "2426", "-a", "5", "-z", "-60", "-d", "1", "-l", "0"),
                         sets["next-word-values-in-one-command"])
        for case_id in NEXT_WORD_CASES:
            with self.subTest(case_id):
                self.assertIn(case_id, sets)

    def test_preset_makes_each_next_word_readback_a_real_change(self):
        for original in ("0", "1"):
            device = FakeUbertooth()
            device.state.update(usr=original, rx=original, tx=original)

            _summary, events = run_test(
                "Radio setting transitions", device, coverage="comprehensive"
            )

            for event in steps(events, step_id="verify-preset"):
                case = event.details["case_id"]
                after = steps(events, step_id="verify-set", case_id=case)[0]
                with self.subTest(original=original, case=case):
                    self.assertNotEqual(event.details["observed"], after.details["observed"])

    def test_detached_values_ignored_as_strays_fail_every_next_word_case(self):
        # Before fork 8b58f75 a detached value was a positional argument that
        # ubertooth-util dropped, and the option only read.
        device = FakeUbertooth(next_word=False, reject_strays=False)

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        self.assertEqual(sorted(NEXT_WORD_CASES), failed_ids(events, "Radio setting transitions"))
        self.assertTrue(all(e.details["cleanup_status"] == "passed"
                            for e in case_results(events)))

    def test_detached_values_refused_as_strays_fail_every_next_word_case(self):
        device = FakeUbertooth(next_word=False)

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertFalse(summary.passed)
        self.assertEqual(sorted(NEXT_WORD_CASES), failed_ids(events, "Radio setting transitions"))
        self.assertIn("command exited 1", summary.failure_reason)

    def test_smoke_has_one_next_word_and_one_pa_zero_case(self):
        _args, smoke = cases_for("Radio setting transitions", "smoke")
        ids = [case.case_id for case in smoke]
        self.assertIn("channel-next-word-value", ids)
        self.assertIn("pa-level-zero-attached", ids)
        self.assertEqual(5, len(ids))

    def test_btle_crc_next_word_cases(self):
        passed, events = run_test("BLE CRC transitions", FakeUbertooth(crc="1"),
                                  coverage="comprehensive")
        self.assertTrue(passed.passed, passed.failure_reason)
        sets = [tuple(e.details["argv"][3:]) for e in steps(events, step_id="set")]
        self.assertEqual([("-k", "1"), ("-k", "0")], sets)

        for device in (FakeUbertooth(crc="1", next_word=False),
                       FakeUbertooth(crc="1", next_word=False, reject_strays=False)):
            failed, events = run_test("BLE CRC transitions", device, coverage="comprehensive")
            with self.subTest(strays=device.reject_strays):
                self.assertFalse(failed.passed)
                self.assertEqual(
                    ["crc-disabled-next-word-value", "crc-enabled-next-word-value"],
                    failed_ids(events, "BLE CRC transitions"),
                )

    def test_util_conflicts_check_the_fork_error(self):
        device = FakeUbertooth()
        original = device._util

        def bare_usage(command, options, on_output):
            result = original(command, options, on_output)
            if "cannot be combined" in result.stderr:
                return CommandResult(result.argv, 1, "", "usage\n", 0.01, False)
            return result

        device._util = bare_usage
        summary, _events = run_test("Ubertooth util conflicts", device)

        self.assertFalse(summary.passed)
        self.assertIn("error: options -q -n cannot be combined", summary.failure_reason)


if __name__ == "__main__":
    unittest.main()
