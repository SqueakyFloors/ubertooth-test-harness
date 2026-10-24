import io
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
    ):
        self.serial = serial
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
            # Repeated -U: the last selection wins (getopt overwrites).
            options = options[options.index("-U") + 2:]
        return self._util(command, options, on_output)

    IDLE_RESET = {"channel": "2441", "squelch": "-120", "usr": "0", "rx": "0", "tx": "0"}

    def _stop_mode(self):
        """Firmware cc2400_idle(): only when leaving an active mode."""
        if self.mode is not None:
            self.state.update(self.IDLE_RESET)
            self.mode = None

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
        if self.api < HOST_API:
            # ubertooth_check_api(): older firmware gets ubertooth_stop().
            self.stops += 1
            self._stop_mode()
            return self._result(
                command, 1, "", "Ubertooth API version found, libubertooth requires\n",
                on_output,
            )
        crc = next((item[2:] for item in options if item.startswith("-v")), None)
        if sum(item in ("-f", "-n", "-p") for item in options) > 1:
            if self.reject_btle_conflicts:
                return self._result(
                    command, 1,
                    "Error: must choose one -f, -n, or -p, pick one pal\n", "",
                    on_output,
                )
            options = tuple(item for item in options if item != "-p")
        crcs = [item[2:] for item in options if item.startswith("-v") and len(item) > 2]
        crc = crcs[-1] if crcs else crc
        capture_lines = self._open_captures(options)
        if "-n" in options or "-f" in options:
            output = capture_lines
            adv = next((item[2:] for item in options if item.startswith("-A")), "37")
            self.state["channel"] = {"37": "2402", "38": "2426", "39": "2480"}[adv]
            self.mode = "ble"
            if crc:
                self.state["crc"] = "0" if self.lose_mode_crc else crc
                output = self._crc_line(crc)
            self._stop_mode()  # ubertooth_stop() on exit sends cmd_stop
            return self._result(
                command, 124, output, "HARD TIMEOUT\n", on_output, timed_out=True
            )
        if crc:
            self.state["crc"] = crc
            return self._result(command, 0, self._crc_line(crc), "", on_output)
        return self._result(
            command, 0, self._crc_line(self.state["crc"]), "", on_output
        )

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
                lines += f"Ignoring extra capture file: {path}\n"
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
        modes = {option for option, _label in transitions._MODE_OPTIONS}
        if len(modes.intersection(options)) > 1:
            if self.reject_conflicts:
                return self._result(command, 1, "", "usage\n", on_output)
            return self._result(command, 0, "Entering LED specan mode\n", "", on_output)
        lines = []
        returncode = 0
        stderr = ""
        if "-r" in options:
            self.state.update(self.BOOT_STATE)
            self.mode = None
            lines.append(f"Resetting ubertooth device {self.serial}")
        for option in options:
            if option == "-S":
                lines.append(f"Stopping ubertooth device {self.serial}")
                self.stops += 1
                self._stop_mode()
            elif option == "-x":
                self.mode = "xmas"
            elif option.startswith("-c") and len(option) > 2:
                self.state["channel"] = option[2:]
            elif option.startswith("-C") and len(option) > 2:
                self.state["channel"] = str(2402 + int(option[2:]))
            elif option.startswith("-a") and len(option) > 2 and option != "-a0":
                if int(option[2:]) < 8:
                    self.state["pa"] = option[2:]
                else:
                    returncode = 1
                    stderr = "control message unsupported\n"
            elif option.startswith("-z") and len(option) > 2:
                self.state["squelch"] = option[2:]
        # ubertooth-util applies -d (all LEDs) before -l (USR LED).
        for option in options:
            if option.startswith("-d") and len(option) > 2:
                self.state.update(usr=option[2:], rx=option[2:], tx=option[2:])
        for option in options:
            if option.startswith("-l") and len(option) > 2:
                self.state["usr"] = option[2:]
        reports = {
            "-C": lambda: (
                f"Current frequency: {self.state['channel']} MHz "
                f"(Bluetooth channel {int(self.state['channel']) - 2402})"
            ),
            "-a": lambda: f"PA Level: {self.state['pa']}",
            "-z": lambda: f"Squelch set to {self.state['squelch']}",
            "-v": lambda: (
                f"Firmware version: {self.firmware} "
                f"(API:{transitions.format_api(self.api)})"
            ),
            "-V": lambda: self.compile_info,
            "-s": lambda: f"Serial No: {self.serial}",
            "-p": lambda: "Part ID: 1234ABCD",
            "-b": lambda: "Board ID Number: 1 (Ubertooth One)",
            "-l": lambda: f"USR LED status: {self.state['usr']}",
            "-d": lambda: (
                f"USR LED status: {self.state['usr']}\n"
                f"RX LED status : {self.state['rx']}\n"
                f"TX LED status : {self.state['tx']}"
            ),
        }
        for option in options:
            if option in reports or option == "-a0":
                lines.append(reports[option[:2]]())
        stdout = "".join(line + "\n" for line in lines)
        return self._result(command, returncode, stdout, stderr, on_output)


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
    """Answer the mandatory host-version preflight, forward everything else."""

    def __init__(self, device):
        self.device = device

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        if tuple(argv) == ("ubertooth-util", "-H"):
            output = "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
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

        composed = {frozenset(case.argv[3:]) for case in cases if len(case.argv) == 5}

        self.assertEqual(
            {frozenset(pair) for pair in compatible_pairs(options)}, composed
        )
        full = next(case for case in cases if case.case_id == "full-readback-composition")
        self.assertEqual(options, full.argv[3:])

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

    def test_repeated_channel_index_is_last_option_wins(self):
        _args, cases = cases_for("Radio setting transitions", "comprehensive")
        case = next(c for c in cases if c.case_id == "channel-index-repeated-set-last-wins")
        argv = next(s for s in case.steps if s.step_id == "set-twice").resolve_argv(
            {"original channel-index": "2441"}
        )

        self.assertEqual(("-C48", "-C24"), argv[3:])
        last = next(s for s in case.steps if s.step_id == "verify-last")
        self.assertEqual("2426", last.observe.expected_value({"original channel-index": "2441"}))

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
                        self.assertNotIn("-vVspb", command)


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
        for pair in ("pcapng-then-pcapng", "pcap-le-phdr-then-pcap-ppi", "pcap-ppi-then-pcap-le-phdr"):
            self.assertIn(f"capture-repeated-{pair}-first-wins", full_ids)
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
        self.assertEqual(3, len([e for e in steps(events, step_id="verify-second-absent")
                                 if e.status == "passed"]))
        first = {
            e.details["case_id"]: e.details["observed"]
            for e in steps(events, step_id="verify-first")
        }
        self.assertEqual(
            ["pcap, link type: 192", "pcap, link type: 256", "pcapng, link type: 256"],
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
            self.assertEqual(["ubertooth-btle", "-U", SERIAL, "-v"], event.details["argv"])

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
        device = FakeUbertooth(fail=lambda argv: argv == ("ubertooth-btle", "-U", SERIAL, "-v"))

        summary, events = run_test("BLE CRC transitions", device)

        self.assertFalse(summary.passed)
        self.assertIn("Read original CRC: command exited 1", summary.failure_reason)
        self.assertNotIn("cleanup failed", summary.failure_reason)
        restore = steps(events, step_id="restore-crc")
        self.assertEqual("skipped", restore[0].status)
        self.assertFalse(any(command[-1] in ("-v0", "-v1") for command in device.commands))

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

    def test_compositions_do_not_repeat_identical_commands(self):
        _args, cases = cases_for("Ubertooth util compositions", "comprehensive")

        commands = [case.argv for case in cases]

        self.assertEqual(len(commands), len({frozenset(c) for c in commands}))

    def test_pa_level_zero_skips_the_case_before_any_change(self):
        device = FakeUbertooth()
        device.state["pa"] = "0"

        summary, events = run_test("Radio setting transitions", device)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual("0", device.state["pa"])
        pa_case = "001-Radio setting transitions-pa-level-mode-stop-restore"
        self.assertEqual(
            ["establish-idle", "read-original-pa-level"],
            [e.details["step_id"] for e in steps(events, stage="transition", case_id=pa_case)],
        )
        self.assertFalse(any(c[-1].startswith("-a") and c[-1] != "-a" for c in device.commands))
        result = next(e for e in case_results(events) if e.details["case_id"] == pa_case)
        self.assertEqual("skipped", result.status)
        self.assertIn("PA level 0 cannot be restored", result.details["reason"])
        restore = steps(events, step_id="restore-pa-level", case_id=pa_case)[0]
        self.assertIn("not needed, nothing was changed", restore.details["reason"])
        finished = next(e for e in events if e.event_type == "test_finished")
        self.assertEqual(1, finished.details["skipped_cases"])
        self.assertEqual(1, summary.skipped_cases)
        self.assertTrue(steps(events, case_id="001-Radio setting transitions-squelch-mode-stop-restore"))

    def test_every_led_is_restored_after_repeated_all_led_set(self):
        device = FakeUbertooth()
        device.state.update(usr="1", rx="1", tx="1")

        summary, events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(("1", "1", "1"), (device.state["usr"], device.state["rx"], device.state["tx"]))
        case = "001-Radio setting transitions-all-leds-repeated-set-last-wins"
        last = steps(events, step_id="verify-last", case_id=case)[0]
        self.assertEqual("0/0/0", last.details["observed"])

    def test_repeated_usr_led_always_changes_the_value(self):
        for original in ("0", "1"):
            device = FakeUbertooth()
            device.state["usr"] = original

            summary, events = run_test(
                "Radio setting transitions", device, coverage="comprehensive"
            )

            case = "001-Radio setting transitions-usr-led-repeated-set-last-wins"
            last = steps(events, step_id="verify-last", case_id=case)[0]
            with self.subTest(original=original):
                self.assertTrue(summary.passed, summary.failure_reason)
                self.assertNotEqual(original, last.details["observed"])

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

    def test_cold_board_pa_zero_skips_only_cases_that_can_change_pa(self):
        device = FakeUbertooth()
        device.state["pa"] = "0"

        radio, radio_events = run_test(
            "Radio setting transitions", device, coverage="comprehensive"
        )
        conflicts, conflict_events = run_test(
            "Ubertooth util conflicts", _cold(FakeUbertooth(reject_conflicts=True))
        )

        self.assertTrue(radio.passed, radio.failure_reason)
        statuses = {e.details["case_id"].split("-", 1)[1]: e.status for e in case_results(radio_events)}
        # PA 0 cannot be written back, so every case that can change PA skips.
        self.assertEqual("skipped", statuses["Radio setting transitions-full-reset-restores-boot-defaults"])
        self.assertEqual("skipped", statuses["Radio setting transitions-pa-level-mode-stop-restore"])
        self.assertEqual("passed", statuses["Radio setting transitions-channel-mode-stop-restore"])
        self.assertEqual("0", device.state["pa"])
        self.assertTrue(conflicts.passed, conflicts.failure_reason)
        skipped = {
            e.details["case_id"].rsplit("conflicts-", 1)[1]
            for e in case_results(conflict_events) if e.status == "skipped"
        }
        self.assertTrue(skipped)
        self.assertTrue(all("range-test" in item or "repeater" in item for item in skipped))
        self.assertEqual(15 - 9, sum(e.status == "passed" for e in case_results(conflict_events)))

    def test_reset_case_moves_pa_off_its_original_and_the_boot_default(self):
        device = FakeUbertooth()
        device.state["pa"] = "5"

        summary, events = run_test("Radio setting transitions", device, coverage="comprehensive")

        self.assertTrue(summary.passed, summary.failure_reason)
        before = steps(events, step_id="verify-pa-level-before-reset")[0]
        self.assertNotIn(before.details["observed"], ("5", "7"))
        self.assertEqual("5", device.state["pa"])

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

        self.assertIn("'must choose one -f, -n, or -p'", summary.failure_reason)

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
            fail=lambda argv: argv == ("ubertooth-btle", "-U", SERIAL, "-v1"),
        )

        summary, events = run_test("BLE CRC transitions", device, cancellation=token)

        self.assertEqual("failed", summary.status)
        self.assertEqual("failed", case_results(events)[0].status)
        self.assertIn("cleanup failed: Restore original CRC", summary.failure_reason)

    def test_cancellation_before_a_step_is_recorded_for_that_step(self):
        token = CancellationToken()
        device = FakeUbertooth(
            crc="1",
            cancel_on=lambda argv: argv[-1] == "-v0",
        )

        summary, events = run_test("BLE CRC transitions", device, cancellation=token)

        self.assertEqual("canceled", summary.status)
        failed_step = case_results(events)[0].details["failed_step"]
        recorded = steps(events, step_id=failed_step)
        self.assertEqual(1, len(recorded))
        self.assertEqual("canceled", recorded[0].status)


if __name__ == "__main__":
    unittest.main()
