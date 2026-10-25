import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import refusal, refusal_cases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Refused command lines"
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
USAGE = {
    "ubertooth-btle": "ubertooth-btle - passive Bluetooth Low Energy monitoring\n",
    "ubertooth-follow": "ubertooth-follow - active(bluez) CLK discovery and follow\n",
    "ubertooth-rx": "ubertooth-rx - passive Classic Bluetooth discovery/decode\n",
    "ubertooth-dump": "ubertooth-dump - output a continuous stream of received bits\n",
    "ubertooth-specan": "ubertooth-specan - output a continuous stream of signal strengths\n",
    "ubertooth-dfu": "ubertooth-dfu - Ubertooth firmware update tool\n",
}
FILE_OPTIONS = {
    "ubertooth-btle": "rqc", "ubertooth-follow": "rqd", "ubertooth-rx": "rqd",
    "ubertooth-dump": "d", "ubertooth-specan": "d", "ubertooth-dfu": "su",
}
VALUE_OPTIONS = {
    "ubertooth-btle": "AxstU", "ubertooth-follow": "lueUbw", "ubertooth-rx": "iluUecst",
    "ubertooth-dump": "U", "ubertooth-specan": "luU", "ubertooth-dfu": "dU",
}


class FakeTools:
    """The six tools' argument handling as at fork f2e06fa (#264, #286): file
    options are named in the option loop and opened after every check, dfu
    -s writes nothing when -d is refused, and "Ignoring extra capture file"
    goes to stderr. ``bugs`` switches on a misbehavior: open-early (a file
    option creates its file in the loop), extra-stdout, temp-left (dfu -u
    leaves its temporary file), sign-first (fork #286, before f2e06fa: dfu -s
    writes its .dfu before the -d image check).
    ``missing`` tools are not installed; ``switch`` False is a library from
    before b6d8ede."""

    def __init__(self, *, bugs=(), missing=(), switch=True):
        self.bugs = set(bugs)
        self.missing = set(missing)
        self.switch = switch
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-H"):
            return self._result(command, 0, "libubertooth 1.1 (git-test), libbtbb 1.0\n")
        if command[0] == sys.executable:
            assert Path(command[1]).name == "refusal.py", command
            if command[2] == "probe":
                if command[3] in self.missing:
                    answer = f"no: {command[3]} is not installed in this run (not on PATH)"
                elif not self.switch:
                    answer = "no: /p/lib/libubertooth.so.1 predates UBERTOOTH_TEST_NO_DEVICES"
                else:
                    answer = "yes (/p/lib/libubertooth.so.1)"
                return self._result(command, 0, f"No-device switch: {answer}\n")
            out = io.StringIO()
            with redirect_stdout(out):
                code = refusal.main(list(command[2:]))
            return self._result(command, code, out.getvalue())
        assert command[:2] == refusal_cases.NO_DEVICES, command
        return self._tool(command, command[2], list(command[3:]))

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _tool(self, command, tool, words):
        assert tool not in self.missing, tool
        out, err = [], []
        files, operands, flags = {}, [], []
        index = 0
        while index < len(words):
            word = words[index]
            if not word.startswith("-") or word == "-":
                operands.append(word)
                index += 1
                continue
            option = word[1]
            if option in FILE_OPTIONS[tool] or option in VALUE_OPTIONS[tool]:
                if index + 1 >= len(words):
                    err.append(f"error: option '-{option}' requires a value\n"
                               if tool == "ubertooth-dump"
                               else f"{tool}: option requires an argument -- {option}\n")
                    return self._result(command, 1, USAGE[tool], "".join(err))
                value = words[index + 1]
                index += 2
                refusal_text = self._value(tool, option, value)
                if refusal_text:
                    return self._result(command, 1, "".join(out),
                                        "".join(err) + refusal_text)
                if option in FILE_OPTIONS[tool]:
                    key = "q" if option in "qc" and tool == "ubertooth-btle" else option
                    if "open-early" in self.bugs and value != "-":
                        self._create(tool, option, value)
                    if key in files and option in "rqc":
                        (out if "extra-stdout" in self.bugs else err).append(
                            f"Ignoring extra capture file: {value}\n")
                    elif key not in files or option in "dsu":
                        files[key] = (option, value)
                else:
                    flags.append((option, value))
                continue
            flags.append((option, None))
            index += 1
        if operands:
            return self._result(command, 1, "".join(out), "".join(err)
                                + f"error: unexpected argument '{operands[0]}'\n")
        if ("h", None) in flags:
            return self._result(command, 0 if tool in ("ubertooth-specan", "ubertooth-dump", "ubertooth-rx") else 1,
                                "".join(out) + USAGE[tool], "".join(err))
        refused = self._after_loop(tool, flags, files)
        if refused:
            return self._result(command, 1, "".join(out), "".join(err) + refused)
        if tool == "ubertooth-dfu":
            return self._dfu(command, files, dict(flags), out, err)
        for option, value in files.values():
            if value == "-":
                out.append("\x00pcap header\x00")
            else:
                self._create(tool, option, value)
        return self._result(command, 1, "".join(out),
                            "".join(err) + NOTE + "could not open Ubertooth device\n")

    @staticmethod
    def _create(tool, option, value):
        if tool == "ubertooth-dfu" and option == "s":
            if Path(value).exists():
                Path(value[:-4] + ".dfu").write_bytes(b"signed")
        elif tool == "ubertooth-dfu":
            Path(value + ".Ab12Cd").write_bytes(b"")
        else:
            Path(value).write_bytes(b"\x0a\x0d\x0d\x0a")

    @staticmethod
    def _value(tool, option, value):
        bad = {
            ("ubertooth-btle", "A"): ("40", "error: advertising channel index must be 37-39 (got 40)\n"),
            ("ubertooth-rx", "c"): ("79", "error: channel must be 0-78 (got 79)\n"),
            ("ubertooth-follow", "e"): ("9", "error: maximum access code errors must be 0-5 (got 9)\n"),
            ("ubertooth-specan", "l"): ("9999", "error: lower frequency must be 2402-2480 MHz (got 9999)\n"),
        }
        if (tool, option) in bad and value == bad[(tool, option)][0]:
            return bad[(tool, option)][1]
        if tool == "ubertooth-dfu" and option == "s" and not value.endswith(".bin"):
            return "Error: -s requires a .bin\n"
        if tool == "ubertooth-btle" and option == "r" and value == "-":
            return "Error: PCAPNG capture to stdout is not supported; use -q - or -c -\n"
        return ""

    @staticmethod
    def _after_loop(tool, flags, files):
        options = [option for option, _value in flags]
        if tool == "ubertooth-btle" and sum(o in options for o in "fnp") > 1:
            return "error: choose one of -f, -n or -p, pick one pal\n"
        if tool == "ubertooth-btle" and ("a", None) in flags and len(
                [o for o in options if o == "a"]) > 1:
            return "error: -a cannot both read and set the access address\n"
        if tool == "ubertooth-rx" and "z" in options and "l" in options:
            return "No address should be specified for survey mode\n"
        if tool == "ubertooth-rx" and "u" in options and "l" not in options:
            return "error: -u <UAP> requires -l <LAP>\n"
        if tool == "ubertooth-specan":
            values = dict(flags)
            if int(values.get("l", 2402)) > int(values.get("u", 2480)):
                return "error: lower frequency must not exceed upper frequency\n"
        return ""

    def _dfu(self, command, files, flags, out, err):
        image = flags.get("d")
        temp = None
        if "u" in files:
            temp = Path(files["u"][1] + ".Ab12Cd")
            temp.write_bytes(b"")
        if "s" in files:
            source = Path(files["s"][1])
            if not source.exists():
                self._drop(temp)
                return self._result(command, 1, "", f"{source}: No such file or directory\n")
            if image is None or "sign-first" in self.bugs:
                Path(str(source)[:-4] + ".dfu").write_bytes(source.read_bytes() + b"suffix")
        if image is not None:
            self._drop(temp)
            return self._result(command, 1, "Checking firmware signature\n",
                                "Too short for a 16-byte DFU suffix\n"
                                "Signature check failed, firmware will not be updated\n")
        self._drop(temp)
        return self._result(command, 1, "", NOTE)

    def _drop(self, temp):
        if temp is not None and "temp-left" not in self.bugs:
            temp.unlink()


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments({"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run(executor, coverage="comprehensive"):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Mac", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events




class HelperTests(unittest.TestCase):

    def _prefix(self, root, library_bytes, name="libubertooth.so.1.1"):
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        tool = root / "bin" / "ubertooth-rx"
        tool.write_text("#!/bin/sh\nexit 99\n")
        tool.chmod(0o755)
        if library_bytes is not None:
            (root / "lib" / name).write_bytes(library_bytes)
        return str(root / "bin")

    def test_probe_reads_the_library_beside_the_tool(self):
        for name in ("libubertooth.so.1.1", "libubertooth.1.1.dylib"):
            with tempfile.TemporaryDirectory() as directory:
                path = self._prefix(Path(directory), b"\0getenv\0UBERTOOTH_TEST_NO_DEVICES\0",
                                    name)
                self.assertTrue(refusal.probe("ubertooth-rx", path).startswith("yes ("))

    def test_probe_rejects_an_old_library_a_missing_one_and_a_missing_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), b"\0getenv\0libusb\0")
            self.assertIn("predates UBERTOOTH_TEST_NO_DEVICES",
                          refusal.probe("ubertooth-rx", path))
            self.assertIn("is not installed", refusal.probe("ubertooth-follow", path))
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), None)
            self.assertIn("no libubertooth beside", refusal.probe("ubertooth-rx", path))

    def test_check_finds_created_changed_missing_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                refusal.main(["seed", directory, "kept.pcap"])
            self.assertEqual("", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap.XYZ").write_bytes(b"")
            self.assertIn("new.pcap.XYZ", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap.XYZ").unlink()
            (root / "new.pcap").write_bytes(b"")
            self.assertIn("new.pcap exists", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap").unlink()
            (root / "kept.pcap").write_bytes(b"")
            self.assertIn("kept.pcap changed", refusal.check(root, [], ["kept.pcap"]))
            (root / "kept.pcap").unlink()
            self.assertIn("kept.pcap is gone", refusal.check(root, [], ["kept.pcap"]))


class CaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = cases("smoke")
        # 3 ways for each of 13 file options (dump has its -U), plus the three
        # extra-capture-file cases.
        self.assertEqual(13 * 3 + 3, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(len(smoke) + 13, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        context = {"capture dir": "/run/captures/x"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            self.assertEqual("probe", case.steps[0].step_id)
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] != sys.executable:
                    # Every tool runs with the switch, one command per step.
                    self.assertEqual(("env", "UBERTOOTH_TEST_NO_DEVICES=1"), argv[:2])
                    self.assertIn(argv[2], USAGE)
                    self.assertTrue(step.name.startswith("env UBERTOOTH_TEST_NO_DEVICES=1 "))
                for path in (a for a in argv if a.startswith("/run")):
                    self.assertTrue(path == "/run/captures/x"
                                    or path.startswith("/run/captures/x/"), path)

    def test_fork_tools_pass_everything(self):
        summary, _events = run(FakeTools(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, _events = run(FakeTools())
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_dfu_signing_before_the_image_check_fails(self):
        summary, events = run(FakeTools(bugs=("sign-first",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["dfu -s <file.bin> -d <bad image> writes no .dfu"],
                         failed_names(events))

    def test_files_opened_in_the_option_loop_fail(self):
        summary, events = run(FakeTools(bugs=("open-early",)), coverage="smoke")
        self.assertFalse(summary.passed)
        failed = failed_names(events)
        # Every file option in every refusal; btle -n -q - -q <file> opened the
        # ignored file too.
        self.assertEqual(13 * 3 + 3, len(failed))

    def test_extra_capture_note_on_stdout_fails(self):
        summary, events = run(FakeTools(bugs=("extra-stdout",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "btle -n -q - -q <file>: the extra-file note is on stderr",
            "follow -q twice and a stray word: the note is on stderr",
            "rx -r twice and a stray word: the note is on stderr",
        ]), failed_names(events))

    def test_dfu_temporary_file_left_behind_fails(self):
        summary, events = run(FakeTools(bugs=("temp-left",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "dfu -u <kept> -d <bad image>", "dfu -u <kept> -s <missing .bin>",
        ]), failed_names(events))

    def test_missing_tool_and_old_library_skip(self):
        tools = FakeTools(missing=("ubertooth-follow",))
        summary, _events = run(tools)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertFalse(any(c[2:3] == ("ubertooth-follow",) for c in tools.commands
                             if c[:2] == refusal_cases.NO_DEVICES))
        old = FakeTools(switch=False)
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertFalse(any(c[:2] == refusal_cases.NO_DEVICES for c in old.commands))


if __name__ == "__main__":
    unittest.main()
